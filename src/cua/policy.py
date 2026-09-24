"""Guardrails: allowlist, action risk classes, Allow / Deny / RequireApproval, redaction.

Stateless decisions over (action, context); indifferent to whether the caller is the LLM,
replay or a human. Anything stateful (action counters, the lease) lives in the session and is
passed in through `ActionContext`.

Risk is never taken on trust: the policy infers it from what is actually being acted on, and
the effective risk is the higher of that and whatever the caller declared (an artifact step's
`risk`). A mismatch is reported, not silently resolved downwards.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal, get_args
from urllib.parse import urlsplit

from pydantic import Field, field_validator

from cua.schema import (
    Action,
    Click,
    ElementInfo,
    Extract,
    Fill,
    Navigate,
    Press,
    Risk,
    Select,
    Status,
)
from cua.schema.common import Model, Slug


class Mode(StrEnum):
    DISCOVERY = "discovery"
    REPLAY = "replay"


class Actor(StrEnum):
    AGENT = "agent"  # the LLM in discovery, the engine in replay
    RUNTIME = "runtime"  # the session itself, e.g. signing on with runtime secrets
    HUMAN = "human"  # an operator; they are the approver, so approval rules don't apply


# Accessible names that commit a change nobody can take back from the UI. Matched
# case-insensitively as a search against the element's name; apps can add their own.
DEFAULT_IRREVERSIBLE_PATTERNS = [
    r"\b(confirm|submit|post|transfer|pay|approve|authori[sz]e|delete|remove|wire|close account)\b",
]


# Every action kind the schema defines, e.g. "click".
ACTION_KINDS: tuple[str, ...] = tuple(
    cls.model_fields["kind"].default for cls in get_args(get_args(Action)[0]))


class UnattendedThresholds(Model):
    """What a stability report must show before an approved capability may run with no
    person watching (see `cua.stability`)."""

    min_runs: int = Field(default=10, gt=0)
    min_success_rate: float = Field(default=1.0, ge=0, le=1)
    max_drift_runs: int = Field(default=0, ge=0)
    max_age_days: int = Field(default=30, gt=0)


class PolicyConfig(Model):
    """Per-app guardrails, reviewable next to the app's state library. Allowed origins are
    per-tenant runtime configuration (each institution hosts the app somewhere else) and are
    passed to `Policy` directly; routes and action kinds belong to the app, so they live here.
    """

    schema_version: Literal["1"] = "1"
    app_id: Slug
    # Path prefixes, matched per segment ("/subacct" allows "/subacct/review", not
    # "/subacctx"). None allows any path on an allowed origin. Denied paths win.
    allowed_paths: list[str] | None = None
    denied_paths: list[str] = []  # plain prefixes, e.g. "/__admin"
    allowed_actions: list[str] = list(ACTION_KINDS)
    irreversible_patterns: list[str] = []
    max_actions: int = Field(default=200, gt=0)
    max_irreversible: int = Field(default=1, ge=0)
    unattended: UnattendedThresholds = UnattendedThresholds()

    @field_validator("allowed_paths", "denied_paths")
    @classmethod
    def _absolute(cls, paths: list[str] | None) -> list[str] | None:
        if bad := [p for p in paths or [] if not p.startswith("/")]:
            raise ValueError(f"paths must start with '/': {bad}")
        return paths

    @field_validator("allowed_actions")
    @classmethod
    def _known_kinds(cls, kinds: list[str]) -> list[str]:
        if unknown := sorted(set(kinds) - set(ACTION_KINDS)):
            raise ValueError(f"unknown action kinds {unknown}; known: {list(ACTION_KINDS)}")
        return kinds

    @field_validator("irreversible_patterns")
    @classmethod
    def _compiles(cls, patterns: list[str]) -> list[str]:
        for p in patterns:
            re.compile(p)
        return patterns


@dataclass(frozen=True)
class ActionContext:
    mode: Mode
    actor: Actor
    action: Action
    element: ElementInfo | None  # the resolved element, if the action has one
    declared_risk: Risk | None = None
    capability_status: Status | None = None
    actions_taken: int = 0
    irreversible_taken: int = 0


@dataclass(frozen=True)
class _Decision:
    rule: str
    reason: str
    risk: Risk  # effective: max(declared, inferred)
    inferred: Risk


@dataclass(frozen=True)
class Allow(_Decision):
    pass


@dataclass(frozen=True)
class Deny(_Decision):
    pass


@dataclass(frozen=True)
class RequireApproval(_Decision):
    pass


Decision = Allow | Deny | RequireApproval


def _highest(*risks: Risk | None) -> Risk:
    return max((r for r in risks if r is not None), key=lambda r: r.rank)


def _under(path: str, prefix: str) -> bool:
    prefix = prefix.rstrip("/")
    return path == prefix or path.startswith(prefix + "/")


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


class Policy:
    def __init__(self, config: PolicyConfig, allowed_origins: Iterable[str]) -> None:
        self.config = config
        self.allowed_origins = frozenset(_origin(o) for o in allowed_origins)
        if not self.allowed_origins:
            raise ValueError("a policy needs at least one allowed origin")
        self._irreversible = [
            re.compile(p, re.IGNORECASE)
            for p in DEFAULT_IRREVERSIBLE_PATTERNS + config.irreversible_patterns
        ]

    def url_violation(self, url: str) -> str | None:
        """Why the engine may not be at `url`, or None if it may."""
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            return f"{url!r} is not an absolute http(s) url"
        if _origin(url) not in self.allowed_origins:
            return f"origin {_origin(url)} is not in the allowlist"
        if any(parts.path.startswith(p) for p in self.config.denied_paths):
            return f"path {parts.path} is denied"
        allowed = self.config.allowed_paths
        if allowed is not None and not any(_under(parts.path, p) for p in allowed):
            return f"path {parts.path} is not in the allowlist"
        return None

    @property
    def allowed_actions(self) -> frozenset[str]:
        return frozenset(self.config.allowed_actions)

    def infer_risk(self, action: Action, element: ElementInfo | None) -> Risk:
        match action:
            case Navigate() | Extract() | Fill() | Select():
                return Risk.READ_ONLY  # nothing is committed until something is submitted
            case Press(key=key) if key not in ("Enter", "Space", " "):
                return Risk.READ_ONLY
            case Click() | Press():
                name = element.name if element else ""
                if any(p.search(name) for p in self._irreversible):
                    return Risk.IRREVERSIBLE
                return Risk.REVERSIBLE
        raise AssertionError(f"unhandled action {action!r}")

    def check(self, ctx: ActionContext) -> Decision:
        inferred = self.infer_risk(ctx.action, ctx.element)
        risk = _highest(inferred, ctx.declared_risk)

        def decide(kind: type[_Decision], rule: str, reason: str) -> Decision:
            return kind(rule=rule, reason=reason, risk=risk, inferred=inferred)  # type: ignore[return-value]

        # Hard limits: no actor, mode or approval gets past these.
        if ctx.action.kind not in self.allowed_actions:
            return decide(Deny, "action_allowlist",
                          f"{ctx.action.kind} actions are not allowed for this app")
        for url in self._destinations(ctx):
            if violation := self.url_violation(url):
                return decide(Deny, "allowlist", violation)
        if (
            isinstance(ctx.action, Fill)
            and ctx.element is not None
            and ctx.element.input_type == "password"
            and ctx.actor is not Actor.RUNTIME
        ):
            return decide(Deny, "credential_entry", "only the runtime enters credentials")
        if ctx.actions_taken >= self.config.max_actions:
            return decide(Deny, "action_budget", f"run exceeded {self.config.max_actions} actions")
        if risk is Risk.IRREVERSIBLE and ctx.irreversible_taken >= self.config.max_irreversible:
            return decide(Deny, "irreversible_budget",
                          f"run already took {ctx.irreversible_taken} irreversible action(s)")

        if ctx.actor is not Actor.AGENT:
            return decide(Allow, "trusted_actor", f"{ctx.actor} actions are not gated by risk")

        # Risk table: what the agent may do on its own, per mode.
        trusted = ctx.mode is Mode.REPLAY and ctx.capability_status is Status.APPROVED
        match risk:
            case Risk.READ_ONLY:
                return decide(Allow, "read_only", "read-only action")
            case Risk.REVERSIBLE if ctx.mode is Mode.DISCOVERY or trusted:
                return decide(Allow, "reversible", "reversible action")
            case Risk.REVERSIBLE:
                return decide(RequireApproval, "unreviewed_capability",
                              "reversible action from a capability that is not approved")
            case Risk.IRREVERSIBLE if ctx.mode is Mode.REPLAY and not trusted:
                return decide(Deny, "unreviewed_capability",
                              "irreversible action from a capability that is not approved")
            case Risk.IRREVERSIBLE:
                return decide(RequireApproval, "irreversible",
                              "irreversible actions need a human's approval every time")
        raise AssertionError(f"unhandled risk {risk!r}")

    @staticmethod
    def _destinations(ctx: ActionContext) -> list[str]:
        """Where this action would take the engine, if knowable before acting."""
        if isinstance(ctx.action, Navigate):
            return [ctx.action.url]
        if isinstance(ctx.action, Click | Press) and ctx.element and ctx.element.href:
            return [ctx.element.href]
        return []


# --- redaction ----------------------------------------------------------------------------

_BACKSTOP_PATTERNS = {
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "card_number": re.compile(r"\b\d(?:[ -]?\d){12,18}\b"),
}


class Redactor:
    """Scrubs sensitive values from anything headed to a sink (evidence, store).

    Two layers: exact values registered for this run (inputs marked pii/confidential, runtime
    secrets, sensitive extracted outputs), matched as whole tokens so short values don't mangle
    unrelated text; and patterns as a backstop for values nobody declared. Screenshots are
    pixels and can't be scrubbed this way; see the web surface's input masking.
    """

    def __init__(self) -> None:
        self._values: dict[str, tuple[re.Pattern[str], str]] = {}

    def register(self, value: str, label: str) -> None:
        value = value.strip()
        if value and value not in self._values:
            pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(value)}(?![A-Za-z0-9])")
            self._values[value] = (pattern, f"«{label}»")

    def labels_in(self, text: str) -> list[str]:
        """Labels of the sensitive values (and backstop patterns) found in `text`."""
        found = [label for pattern, label in self._values.values() if pattern.search(text)]
        found += [f"«{name}»" for name, pattern in _BACKSTOP_PATTERNS.items()
                  if pattern.search(text)]
        return sorted(set(found))

    def scrub(self, text: str) -> str:
        # Longest first, so a value containing another is replaced whole.
        for value in sorted(self._values, key=len, reverse=True):
            pattern, mask = self._values[value]
            text = pattern.sub(mask, text)
        for name, pattern in _BACKSTOP_PATTERNS.items():
            text = pattern.sub(f"«{name}»", text)
        return text

    def redact(self, obj: Any) -> Any:
        match obj:
            case str():
                return self.scrub(obj)
            case Mapping():
                return {k: self.redact(v) for k, v in obj.items()}
            case list() | tuple():
                return [self.redact(v) for v in obj]
            case int() | float() | Decimal() if not isinstance(obj, bool):
                scrubbed = self.scrub(str(obj))
                return obj if scrubbed == str(obj) else scrubbed
        return obj
