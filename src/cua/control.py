"""Control transfer: the lease on a live session (who holds it, epoch), intervention
requests, and the resume/abort signal. The operator UI is an adapter over this module.

Whoever acquires the lease invalidates every earlier token. The session checks its token
before resolving and again immediately before acting, so an operator who takes over — even
while an approval is pending — is never raced by the agent.

Two ways for a human to step in uninvited:
- pause (cooperative): the agent hands over at its next action, and the run continues after
  the human hands back;
- take-over (hard): the lease moves to the operator; the agent's next action fails and the
  run ends as aborted.
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

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
    id: str = ""
    evidence_ref: str | None = None  # "<run_id>/snapshots/NNNN-label", without extension


@dataclass(frozen=True)
class InterventionRequest:
    id: str
    run_id: str
    step_id: str | None
    reason: str
    evidence_ref: str | None = None


@dataclass(frozen=True)
class Resolution:
    outcome: Literal["resumed", "aborted"]
    operator: str | None = None
    actions: tuple[HumanAction, ...] = ()
    note: str = ""


class ControlPort(Protocol):
    def acquire(self, holder: str) -> LeaseToken: ...

    def is_current(self, token: LeaseToken) -> bool: ...

    def consume_pause(self) -> bool:
        """True (once) if an operator asked the agent to pause and hand over."""

    def request_approval(self, request: ApprovalRequest, timeout_s: float) -> bool:
        """Block until a human approves or rejects. No answer in time is a rejection."""

    def request_intervention(self, request: InterventionRequest, timeout_s: float) -> Resolution:
        """Hand the live session to a human; block until they resume or abort. No answer in
        time is an abort."""


class InMemoryControl:
    """Lease bookkeeping plus scripted operator answers: for tests and unattended runs, where
    it fails closed (reject every approval, abort every intervention)."""

    def __init__(
        self,
        approve: Callable[[ApprovalRequest], bool] | None = None,
        intervene: Callable[[InterventionRequest], Resolution] | None = None,
    ) -> None:
        self._approve = approve or (lambda _: False)
        self._intervene = intervene or (lambda _: Resolution("aborted", note="no operator"))
        self._lock = threading.Lock()
        self._current = LeaseToken(holder="", epoch=0)
        self._pause = False
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

    def pause(self) -> None:
        self._pause = True

    def consume_pause(self) -> bool:
        with self._lock:
            paused, self._pause = self._pause, False
            return paused

    def request_approval(self, request: ApprovalRequest, timeout_s: float) -> bool:
        self.approvals.append(request)
        return self._approve(request)

    def request_intervention(self, request: InterventionRequest, timeout_s: float) -> Resolution:
        self.interventions.append(request)
        return self._intervene(request)


# --- the operator desk ----------------------------------------------------------------------

ItemStatus = Literal["pending", "approved", "rejected", "resumed", "aborted", "expired"]


@dataclass
class DeskItem:
    kind: Literal["approval", "intervention"]
    request: ApprovalRequest | InterventionRequest
    created_at: datetime
    status: ItemStatus = "pending"
    operator: str | None = None
    note: str = ""
    resolved_at: datetime | None = None
    _done: threading.Event = field(default_factory=threading.Event, repr=False)

    def view(self) -> dict[str, Any]:
        r = self.request
        return {
            "id": r.id, "kind": self.kind, "status": self.status, "run_id": r.run_id,
            "step_id": r.step_id, "reason": r.reason, "evidence_ref": r.evidence_ref,
            "action": getattr(r, "action", None),
            "risk": str(r.risk) if isinstance(r, ApprovalRequest) else None,
            "created_at": self.created_at.isoformat(), "operator": self.operator,
            "note": self.note,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
        }


class DeskError(Exception):
    pass


class OperatorDesk(InMemoryControl):
    """A thread-safe `ControlPort` for real operators: requests wait on the desk until an
    operator resolves them (from any thread, e.g. the web console) or they time out, which
    fails closed."""

    def __init__(self) -> None:
        super().__init__()
        self._items: dict[str, DeskItem] = {}

    # the agent's side (blocks the session's thread)

    def request_approval(self, request: ApprovalRequest, timeout_s: float) -> bool:
        item = self._post("approval", request)
        return self._wait(item, timeout_s) == "approved"

    def request_intervention(self, request: InterventionRequest, timeout_s: float) -> Resolution:
        item = self._post("intervention", request)
        status = self._wait(item, timeout_s)
        if status == "resumed":
            return Resolution("resumed", item.operator, note=item.note)
        note = item.note if status == "aborted" else "no operator responded in time"
        return Resolution("aborted", item.operator, note=note)

    def _post(self, kind: Literal["approval", "intervention"],
              request: ApprovalRequest | InterventionRequest) -> DeskItem:
        if not request.id:
            raise DeskError("requests on the desk need an id")
        item = DeskItem(kind, request, datetime.now(UTC))
        with self._lock:
            self._items[request.id] = item
        return item

    def _wait(self, item: DeskItem, timeout_s: float) -> ItemStatus:
        if not item._done.wait(timeout_s):
            with self._lock:
                if item.status == "pending":
                    item.status, item.resolved_at = "expired", datetime.now(UTC)
        return item.status

    # the operator's side (any thread)

    def resolve_approval(self, item_id: str, approve: bool, operator: str) -> None:
        self._resolve(item_id, "approval", "approved" if approve else "rejected", operator, "")

    def resolve_intervention(self, item_id: str, outcome: Literal["resumed", "aborted"],
                             operator: str, note: str = "") -> None:
        self._resolve(item_id, "intervention", outcome, operator, note)

    def _resolve(self, item_id: str, kind: str, status: ItemStatus, operator: str,
                 note: str) -> None:
        if not operator.strip():
            raise DeskError("operator name is required")
        with self._lock:
            item = self._items.get(item_id)
            if item is None or item.kind != kind:
                raise DeskError(f"no {kind} {item_id!r}")
            if item.status != "pending":
                raise DeskError(f"{item_id} is already {item.status}")
            item.status, item.operator, item.note = status, operator, note
            item.resolved_at = datetime.now(UTC)
        item._done.set()

    def take_over(self, operator: str) -> LeaseToken:
        if not operator.strip():
            raise DeskError("operator name is required")
        return self.acquire(f"operator:{operator}")

    def state(self) -> dict[str, Any]:
        with self._lock:
            items = sorted(self._items.values(), key=lambda i: i.created_at, reverse=True)
            return {"holder": self._current.holder, "epoch": self._current.epoch,
                    "pause_requested": self._pause, "items": [i.view() for i in items]}
