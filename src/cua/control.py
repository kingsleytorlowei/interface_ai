"""Control transfer: the lease on a live session (who holds it, epoch), intervention
requests, and the resume/abort signal. The operator UI is an adapter over this module.

Whoever acquires the lease invalidates every earlier token. The session checks its token
before resolving and again immediately before acting, so an operator who takes over — even
while an approval is pending — is never raced by the agent.
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from cua.schema import HumanAction, Risk


@dataclass(frozen=True)
class LeaseToken:
    holder: str
    epoch: int


@dataclass(frozen=True)
class ApprovalRequest:
    run_id: str
    step_id: str | None
    action: str  # human-readable, already redacted
    risk: Risk
    reason: str


@dataclass(frozen=True)
class InterventionRequest:
    id: str
    run_id: str
    step_id: str | None
    reason: str


@dataclass(frozen=True)
class Resolution:
    outcome: Literal["resumed", "aborted"]
    operator: str | None = None
    actions: tuple[HumanAction, ...] = ()
    note: str = ""


class ControlPort(Protocol):
    def acquire(self, holder: str) -> LeaseToken: ...

    def is_current(self, token: LeaseToken) -> bool: ...

    def request_approval(self, request: ApprovalRequest, timeout_s: float) -> bool:
        """Block until a human approves or rejects. No answer in time is a rejection."""

    def request_intervention(self, request: InterventionRequest, timeout_s: float) -> Resolution:
        """Hand the live session to a human; block until they resume or abort. No answer in
        time is an abort."""


class InMemoryControl:
    """Lease bookkeeping plus scripted operator answers: for tests and unattended runs, where
    it fails closed (reject every approval, abort every intervention). The operator console
    is another implementation of the same port."""

    def __init__(
        self,
        approve: Callable[[ApprovalRequest], bool] | None = None,
        intervene: Callable[[InterventionRequest], Resolution] | None = None,
    ) -> None:
        self._approve = approve or (lambda _: False)
        self._intervene = intervene or (lambda _: Resolution("aborted", note="no operator"))
        self._lock = threading.Lock()
        self._current = LeaseToken(holder="", epoch=0)
        self.approvals: list[ApprovalRequest] = []
        self.interventions: list[InterventionRequest] = []

    @property
    def holder(self) -> str:
        return self._current.holder

    def acquire(self, holder: str) -> LeaseToken:
        with self._lock:
            self._current = LeaseToken(holder, self._current.epoch + 1)
            return self._current

    def is_current(self, token: LeaseToken) -> bool:
        with self._lock:
            return token == self._current

    def request_approval(self, request: ApprovalRequest, timeout_s: float) -> bool:
        self.approvals.append(request)
        return self._approve(request)

    def request_intervention(self, request: InterventionRequest, timeout_s: float) -> Resolution:
        self.interventions.append(request)
        return self._intervene(request)
