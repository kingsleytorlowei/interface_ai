"""`GuardedSession` — the single chokepoint through which every action flows.

policy.check -> lease.check -> surface.act -> evidence.emit. Discovery and replay are drivers
on top of this; neither can reach the surface directly.

One pipeline for every action, whoever asked for it:

    lease precheck -> resolve (no side effects) -> policy.check -> approval, if required
    -> lease check (a human may have taken over while approval was pending) -> act -> settle
    -> observe -> post-check (still inside the allowlist?) -> evidence

Every stage emits a redacted event, including refusals. The session does not interpret what
it observes: outcome classification belongs to replay.
"""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from cua.apps import predicate_holds
from cua.control import ApprovalRequest, ControlPort, InterventionRequest, LeaseToken
from cua.evidence import EventKind, RunLog, snapshot_digest
from cua.policy import ActionContext, Actor, Decision, Deny, Mode, Policy, RequireApproval
from cua.schema import (
    Action,
    AppModel,
    Click,
    ElementInfo,
    Extract,
    FailureCategory,
    Fill,
    HumanAction,
    Intervention,
    Navigate,
    Observation,
    Press,
    Risk,
    Select,
    Sensitivity,
    Status,
    Target,
    render_template,
)
from cua.secrets import SecretProvider
from cua.surface import ActionTimeout, ResolutionError, Resolved, Surface

# --- errors -------------------------------------------------------------------------------


class SessionError(Exception):
    """Anything the session refused or failed to do. `category` maps straight onto a replay
    `Failure`; None means a human decided (rejected an approval, took control), which ends
    in an abort or a handoff rather than a failure."""

    category: FailureCategory | None = FailureCategory.APP_ERROR
    step_id: str | None = None  # where it happened, if not inside a driver's step (sign-on)


class PolicyDenied(SessionError):
    category = FailureCategory.POLICY_DENIED

    def __init__(self, message: str, rule: str) -> None:
        super().__init__(message)
        self.rule = rule


class ApprovalRejected(SessionError):
    category = None


class LeaseLost(SessionError):
    category = None


class InterventionAborted(SessionError):
    """An operator paused the run, took the session, and chose to end it."""

    category = None


class TargetError(SessionError):
    def __init__(self, error: ResolutionError) -> None:
        super().__init__(str(error))
        self.kind = error.kind
        self.attempts = error.attempts
        self.category = (
            FailureCategory.TARGET_AMBIGUOUS
            if error.kind in ("ambiguous", "fingerprint_mismatch")
            else FailureCategory.TARGET_NOT_FOUND
        )


class ActionFailed(SessionError):
    def __init__(self, message: str, risk: Risk, *, step_id: str | None = None,
                 timed_out: bool = False) -> None:
        super().__init__(message)
        self.risk = risk  # the attempt may have taken effect before it failed
        self.step_id = step_id
        self.category = FailureCategory.TIMEOUT if timed_out else FailureCategory.APP_ERROR


class SignOnFailed(SessionError):
    category = FailureCategory.APP_ERROR
    step_id = "sign_on"


# --- commands -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Ref:
    """An element from the latest observation, as the LLM refers to it."""

    ref: str


@dataclass(frozen=True)
class Command:
    action: Action  # templates already rendered by the driver
    target: Target | Ref | None = None  # replay passes a Target, discovery a Ref
    declared_risk: Risk | None = None  # the artifact's claim; policy may only raise it
    step_id: str | None = None
    sensitivity: Sensitivity = Sensitivity.INTERNAL  # of the value filled or extracted
    timeout_ms: int = 5000

    def __post_init__(self) -> None:
        if isinstance(self.action, Navigate) and self.target is not None:
            raise ValueError("navigate takes no target")
        if not isinstance(self.action, Navigate | Press) and self.target is None:
            raise ValueError(f"{self.action.kind} needs a target")


@dataclass(frozen=True)
class ActionReport:
    effective_risk: Risk
    target: Target | None  # for a Ref: the synthesized Target that was actually used
    strategy_index: int  # > 0: a fallback strategy won (drift signal)
    text: str | None  # raw text read by an Extract; parsing is the driver's job
    after: Observation


# --- session ------------------------------------------------------------------------------


class GuardedSession:
    def __init__(
        self,
        *,
        surface: Surface,
        app: AppModel,
        policy: Policy,
        control: ControlPort,
        log: RunLog,
        secrets: SecretProvider,
        env: Mapping[str, str],
        mode: Mode,
        capability_status: Status | None = None,
        approval_timeout_s: float = 300,
        intervention_timeout_s: float = 1800,
        audit_targets: bool = False,
    ) -> None:
        self.app = app
        self.log = log
        self.mode = mode
        self.capability_status = capability_status
        self.holder = f"agent:{log.run_id}"
        self._surface = surface
        self._policy = policy
        self._control = control
        self._secrets = secrets
        self.env = dict(env)  # per-tenant runtime values for templates
        self._approval_timeout_s = approval_timeout_s
        self._intervention_timeout_s = intervention_timeout_s
        self._token: LeaseToken | None = None
        self._actions = 0
        self._irreversible = 0
        self._approvals = 0
        self.interventions: list[Intervention] = []  # every handoff in this session, in order
        # With `audit_targets`, per target slug: which of its strategies identified the
        # resolved element on every resolution so far (verification uses this to drop
        # fallbacks that don't generalise).
        self._audit_targets = audit_targets
        self.target_audits: dict[str, list[bool]] = {}
        self._frozen: str | None = None
        self._last_digest: str | None = None
        self.signed_on = False

    @classmethod
    @contextmanager
    def open(cls, **kwargs: Any) -> Iterator["GuardedSession"]:
        session = cls(**kwargs)
        session.log.emit(
            EventKind.SESSION_OPENED,
            mode=session.mode,
            app_id=session.app.app_id,
            capability_status=session.capability_status,
            allowed_origins=sorted(session._policy.allowed_origins),
            allowed_paths=session._policy.config.allowed_paths,
            allowed_actions=sorted(session._policy.allowed_actions),
        )
        session._acquire()
        outcome = "error"
        try:
            yield session
            outcome = "ok"
        finally:
            session.log.emit(EventKind.SESSION_CLOSED, outcome=outcome,
                             actions=session._actions, irreversible=session._irreversible)

    @property
    def run_id(self) -> str:
        return self.log.run_id

    # perception -------------------------------------------------------------------------

    @property
    def allowed_actions(self) -> frozenset[str]:
        """Action kinds the policy permits here (drivers offer nothing else)."""
        return self._policy.allowed_actions

    def observe(self, *, quiet: bool = False) -> Observation:
        """`quiet` logs the observation only if the screen changed (for polling loops)."""
        try:
            obs = self._surface.observe()
        except ActionTimeout as e:
            self.log.emit(EventKind.ACTION_FAILED, stage="observe", error=str(e))
            raise ActionFailed(f"observe failed: {e}", Risk.READ_ONLY, timed_out=True) from e
        digest = snapshot_digest(obs)
        if not quiet or digest != self._last_digest:
            self.log.emit(EventKind.OBSERVATION, url=obs.url, title=obs.title,
                          frames=obs.frame_urls, snapshot=digest)
        self._last_digest = digest
        return obs

    def capture(self, label: str, step_id: str | None = None) -> str | None:
        """Best-effort failure snapshot (tree, text, masked screenshot); returns its path.
        Doesn't wait long for quiet: the page may be the one that hung."""
        try:
            obs = self._surface.observe(settle_timeout_ms=1000)
        except Exception:
            obs = None
        try:
            shot = self._surface.screenshot()
        except Exception:
            shot = None
        if obs is None and shot is None:
            return None
        return self.log.snapshot(label, step_id=step_id, observation=obs, screenshot=shot)

    def mark_sensitive(self, value: str, label: str) -> None:
        self.log.redactor.register(value, label)

    # action -----------------------------------------------------------------------------

    def execute(self, cmd: Command) -> ActionReport:
        return self._execute(cmd, Actor.AGENT)

    def _execute(self, cmd: Command, actor: Actor) -> ActionReport:
        step = cmd.step_id
        if self._frozen:
            raise PolicyDenied(f"session is frozen: {self._frozen}", "frozen")
        if actor is Actor.AGENT and self._control.consume_pause():
            intervention = self.request_intervention("an operator asked to pause", step)
            if intervention.resolution == "aborted":
                raise InterventionAborted(f"operator ended the run while paused at {step}")
        if cmd.sensitivity.masked_in_logs and isinstance(cmd.action, Fill | Select):
            value = cmd.action.value if isinstance(cmd.action, Fill) else cmd.action.option
            self.mark_sensitive(value, cmd.action.target)

        self._check_lease(step)
        resolved, target, element = self._resolve(cmd)
        if isinstance(cmd.action, Extract) and cmd.sensitivity.masked_in_logs and element:
            # An extracted element's accessible name is the data itself, and it is logged
            # (decision, action) before the value is read.
            self.mark_sensitive(element.name, cmd.action.into)

        decision = self._policy.check(ActionContext(
            mode=self.mode,
            actor=actor,
            action=cmd.action,
            element=element,
            declared_risk=cmd.declared_risk,
            capability_status=self.capability_status,
            actions_taken=self._actions,
            irreversible_taken=self._irreversible,
        ))
        self._log_decision(cmd, actor, decision, element)
        if isinstance(decision, Deny):
            raise PolicyDenied(decision.reason, decision.rule)
        if isinstance(decision, RequireApproval):
            self._await_approval(cmd, decision, element)

        self._check_lease(step)
        self.log.emit(EventKind.ACTION_STARTED, step, actor=actor, action=cmd.action,
                      element=element, risk=decision.risk)
        # Counted before acting: a failed attempt may still have had its effect.
        self._actions += 1
        if decision.risk is Risk.IRREVERSIBLE:
            self._irreversible += 1
        try:
            text = self._perform(cmd.action, resolved)
            self._surface.settle()
        except Exception as e:
            self.log.emit(EventKind.ACTION_FAILED, step, stage="act",
                          error=f"{type(e).__name__}: {e}")
            self.capture("act-failed", step)
            # The full error is in the event log; the result carries its first line.
            first_line = (str(e).splitlines() or [type(e).__name__])[0]
            raise ActionFailed(f"{cmd.action.kind} failed: {first_line}", decision.risk,
                               step_id=step, timed_out=isinstance(e, ActionTimeout)) from e

        after = self.observe()
        if violation := self._off_allowlist(after):
            self._frozen = violation
            self.log.emit(EventKind.POLICY_DECISION, step, verdict="deny",
                          rule="allowlist_after", reason=violation)
            self.capture("left-allowlist", step)
            raise PolicyDenied(violation, "allowlist")

        if text is not None and cmd.sensitivity.masked_in_logs:
            assert isinstance(cmd.action, Extract)
            self.mark_sensitive(text, cmd.action.into)
        strategy_index = resolved.strategy_index if resolved else 0
        self.log.emit(EventKind.ACTION_SUCCEEDED, step, strategy_index=strategy_index, text=text,
                      target=target if isinstance(cmd.target, Ref) else None)
        return ActionReport(decision.risk, target, strategy_index, text, after)

    def _resolve(self, cmd: Command) -> tuple[Resolved | None, Target | None, ElementInfo | None]:
        if cmd.target is None:
            return None, None, None
        try:
            if isinstance(cmd.target, Ref):
                # Act through the synthesized Target, not the ref: whatever discovery records
                # has then already resolved once on the live surface.
                purpose: Literal["act", "extract"] = (
                    "extract" if isinstance(cmd.action, Extract) else "act")
                pinned = self._surface.pin(cmd.target.ref)
                target = self._surface.synthesize_target(pinned, purpose)
            else:
                target = cmd.target
            resolved = self._surface.resolve(target, cmd.timeout_ms)
        except ResolutionError as e:
            self.log.emit(EventKind.ACTION_FAILED, cmd.step_id, stage="resolve", kind=e.kind,
                          detail=e.detail, attempts=e.attempts)
            self.capture("resolve-failed", cmd.step_id)
            raise TargetError(e) from e
        slug = getattr(cmd.action, "target", None)
        if self._audit_targets and isinstance(cmd.target, Target) and slug:
            self._audit(slug, target, resolved, cmd.step_id)
        return resolved, target, self._surface.describe(resolved)

    def _audit(self, slug: str, target: Target, resolved: Resolved, step: str | None) -> None:
        try:
            checks = self._surface.audit(target, resolved)
        except Exception as e:  # evidence for a later decision; never fails the action
            self.log.emit(EventKind.TARGET_AUDIT, step, target=slug, error=str(e))
            return
        before = self.target_audits.get(slug, [True] * len(checks))
        self.target_audits[slug] = [a and b for a, b in zip(before, checks, strict=True)]
        self.log.emit(EventKind.TARGET_AUDIT, step, target=slug, strategies=checks)

    def _perform(self, action: Action, el: Resolved | None) -> str | None:
        match action:
            case Navigate(url=url):
                self._surface.navigate(url)
            case Click():
                self._surface.click(_required(el))
            case Fill(value=value):
                self._surface.fill(_required(el), value)
            case Select(option=option):
                self._surface.select(_required(el), option)
            case Press(key=key):
                self._surface.press(key, el)
            case Extract():
                return self._surface.read_text(_required(el))
        return None

    def _off_allowlist(self, obs: Observation) -> str | None:
        for url in [obs.url, *obs.frame_urls]:
            if urlsplit(url).scheme in ("http", "https"):
                if violation := self._policy.url_violation(url):
                    return violation
        return None

    def _log_decision(
        self, cmd: Command, actor: Actor, decision: Decision, element: ElementInfo | None
    ) -> None:
        verdict = {Deny: "deny", RequireApproval: "require_approval"}.get(type(decision), "allow")
        self.log.emit(EventKind.POLICY_DECISION, cmd.step_id, verdict=verdict,
                      rule=decision.rule, reason=decision.reason, actor=actor,
                      action=cmd.action, element=element, risk=decision.risk,
                      inferred=decision.inferred, declared=cmd.declared_risk)
        if cmd.declared_risk and decision.inferred.rank > cmd.declared_risk.rank:
            self.log.emit(EventKind.RISK_MISMATCH, cmd.step_id, declared=cmd.declared_risk,
                          inferred=decision.inferred)

    def _await_approval(
        self, cmd: Command, decision: Decision, element: ElementInfo | None
    ) -> None:
        self._approvals += 1
        request = ApprovalRequest(
            run_id=self.run_id,
            step_id=cmd.step_id,
            action=self.log.redactor.scrub(_describe(cmd.action, element)),
            risk=decision.risk,
            reason=decision.reason,
            id=f"{self.run_id}-ap{self._approvals}",
            evidence_ref=self._evidence_ref(self.capture("approval", cmd.step_id)),
        )
        self.log.emit(EventKind.APPROVAL_REQUESTED, cmd.step_id, action=request.action,
                      risk=request.risk, reason=request.reason)
        approved = self._control.request_approval(request, self._approval_timeout_s)
        self.log.emit(EventKind.APPROVAL_RESOLVED, cmd.step_id, approved=approved)
        if not approved:
            raise ApprovalRejected(f"approval rejected: {request.action}")

    # control ----------------------------------------------------------------------------

    def _acquire(self) -> None:
        self._token = self._control.acquire(self.holder)
        self.log.emit(EventKind.LEASE_ACQUIRED, holder=self._token.holder,
                      epoch=self._token.epoch)

    def _check_lease(self, step_id: str | None) -> None:
        if self._token is None:
            raise LeaseLost("session was not opened; use GuardedSession.open")
        if not self._control.is_current(self._token):
            self.log.emit(EventKind.LEASE_LOST, step_id, epoch=self._token.epoch)
            raise LeaseLost("another party holds control of this session")

    def request_intervention(self, reason: str, step_id: str | None = None) -> Intervention:
        """Hand the live session to a human and block until they resume or abort. After a
        resume the agent holds a fresh lease; refs from before the handoff are stale."""
        request = InterventionRequest(
            id=f"{self.run_id}-iv{len(self.interventions) + 1}",
            run_id=self.run_id,
            step_id=step_id,
            reason=self.log.redactor.scrub(reason),
            evidence_ref=self._evidence_ref(self.capture("intervention", step_id)),
        )
        requested_at = datetime.now(UTC)
        self.log.emit(EventKind.INTERVENTION_REQUESTED, step_id, id=request.id,
                      reason=request.reason, evidence_ref=request.evidence_ref)
        self._surface.start_capture()
        try:
            resolution = self._control.request_intervention(request,
                                                            self._intervention_timeout_s)
        finally:
            captured = self._surface.drain_captured()
        actions = [self._redact_human(a) for a in [*captured, *resolution.actions]]
        if resolution.outcome == "resumed":
            self._acquire()
        self.log.emit(EventKind.INTERVENTION_RESOLVED, step_id, id=request.id,
                      outcome=resolution.outcome, operator=resolution.operator,
                      actions=actions, note=resolution.note)
        intervention = Intervention(
            id=request.id,
            step_id=step_id,
            reason=request.reason,
            operator=resolution.operator,
            actions=actions,
            resolution=resolution.outcome,
            requested_at=requested_at,
            resolved_at=datetime.now(UTC),
        )
        self.interventions.append(intervention)
        return intervention

    def _redact_human(self, action: HumanAction) -> HumanAction:
        """What a person typed may be anything (unregistered PII included): keep that a field
        was filled and how long the entry was, not the text. Dropdown choices come from a fixed
        list and are kept."""
        value = action.value
        if action.kind == "fill" and value is not None and value != "***":
            value = f"«{len(value)} characters»"
        scrub = self.log.redactor.scrub
        return action.model_copy(update={
            "target_description": scrub(action.target_description),
            "value": scrub(value) if value is not None else None,
        })

    def _evidence_ref(self, snapshot: str | None) -> str | None:
        return f"{self.run_id}/{snapshot}" if snapshot else None

    # sign-on ----------------------------------------------------------------------------

    def sign_on(self) -> None:
        """Establish an application session with runtime secrets. Used at entry and as the
        `reauthenticate` recovery; the LLM never sees or types credentials."""
        spec = self.app.sign_on
        if spec is None:
            raise SignOnFailed(f"app {self.app.app_id!r} has no sign_on spec")
        try:
            username = self._secrets.get(spec.username_secret)
            password = self._secrets.get(spec.password_secret)
        except KeyError as e:
            raise SignOnFailed(str(e)) from e
        self.mark_sensitive(username, spec.username_secret)
        self.mark_sensitive(password, spec.password_secret)

        step = "sign_on"
        commands = [
            Command(Navigate(url=render_template(spec.url, env=self.env)), step_id=step),
            Command(Fill(target="username", value=username), spec.username, step_id=step),
            Command(Fill(target="password", value=password), spec.password, step_id=step),
            Command(Click(target="submit"), spec.submit, step_id=step),
        ]
        for cmd in commands:
            report = self._execute(cmd, Actor.RUNTIME)
        if not all(predicate_holds(p, report.after) for p in spec.success):
            self.capture("sign-on-failed", step)
            raise SignOnFailed("sign-on did not reach the expected screen")
        self.signed_on = True
        self.log.emit(EventKind.SIGNED_ON, step)


def _required(el: Resolved | None) -> Resolved:
    assert el is not None, "Command validation guarantees a target for this action"
    return el


def _describe(action: Action, element: ElementInfo | None) -> str:
    what = f'{element.role} "{element.name}"' if element else "the page"
    match action:
        case Navigate(url=url):
            return f"navigate to {url}"
        case Click():
            return f"click {what}"
        case Fill(value=value):
            return f"fill {what} with {value!r}"
        case Select(option=option):
            return f"select {option!r} in {what}"
        case Press(key=key):
            return f"press {key} on {what}"
        case Extract():
            return f"read {what}"
    raise AssertionError(f"unhandled action {action!r}")
