"""The surface port: how the engine perceives and acts on an application, whatever it is
(modern web, legacy web, desktop). Everything above this line speaks `Target`s and
`Observation`s; everything below is adapter-specific.
"""

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from cua.schema import ElementInfo, HumanAction, Observation, Target


@dataclass(frozen=True)
class Resolved:
    """A live element found for a target. `handle` is adapter-specific and must not leave the
    session layer. `strategy_index` > 0 means a fallback strategy won (drift signal)."""

    handle: Any
    strategy_index: int
    description: str


class ResolutionError(Exception):
    def __init__(
        self,
        kind: Literal["not_found", "ambiguous", "fingerprint_mismatch", "frame_not_found"],
        detail: str,
        attempts: list[str] | None = None,
    ) -> None:
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail
        self.attempts = attempts or []


class ActionTimeout(Exception):
    """The surface did not respond within its action budget (a slow or hung load). Adapters
    raise this rather than their own timeout types, so the engine can treat slowness as a
    recoverable condition on any surface."""


@dataclass
class Pinned:
    """An element the LLM referred to by ref, pinned before refs can go stale."""

    handle: Any
    role: str
    name: str


class Surface(Protocol):
    def observe(self, settle_timeout_ms: int = 5000) -> Observation:
        """The current screen, after waiting up to `settle_timeout_ms` for it to go quiet."""
        ...

    def screenshot(self) -> bytes:
        """Capture the screen with sensitive inputs (e.g. passwords) masked."""

    def navigate(self, url: str) -> None: ...

    def settle(self, timeout_ms: int = 5000) -> None:
        """Wait until the surface is quiescent after an action (no in-flight loads)."""

    def pin(self, ref: str) -> Pinned:
        """Pin the element behind a ref from the latest observation."""

    def synthesize_target(self, pinned: Pinned, purpose: Literal["act", "extract"]) -> Target:
        """Build a semantic target for a pinned element; every strategy kept is verified to
        resolve uniquely to that same element on the live surface."""

    def resolve(self, target: Target, timeout_ms: int = 5000) -> Resolved: ...

    def audit(self, target: Target, el: Resolved) -> list[bool]:
        """For each of the target's strategies (not just the one that won), whether it alone
        identifies exactly the resolved element. Used to prove fallbacks generalise."""
        ...

    def describe(self, el: Resolved) -> ElementInfo:
        """What an operator would see of a resolved element (what policy judges it by)."""

    def click(self, el: Resolved) -> None: ...

    def fill(self, el: Resolved, value: str) -> None: ...

    def select(self, el: Resolved, option: str) -> None: ...

    def press(self, key: str, el: Resolved | None = None) -> None: ...

    def read_text(self, el: Resolved) -> str: ...

    def start_capture(self) -> None:
        """Begin recording what a human does on the surface (during a handoff only, so the
        engine's own actions are never attributed to a person)."""

    def drain_captured(self) -> list[HumanAction]:
        """Stop recording and return the human's actions (password values masked)."""
