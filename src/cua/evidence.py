"""Run directories, redacted JSONL event log, and failure snapshots (screenshot + tree).

Every write goes through the run's `Redactor`: this is where redaction is enforced, so nothing
upstream has to remember to do it.
"""

import hashlib
import json
import secrets
import threading
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Any

from pydantic import BaseModel

from cua.policy import Redactor
from cua.schema import Observation


class EventKind(StrEnum):
    SESSION_OPENED = "session_opened"
    SESSION_CLOSED = "session_closed"
    LEASE_ACQUIRED = "lease_acquired"
    LEASE_LOST = "lease_lost"
    OBSERVATION = "observation"
    POLICY_DECISION = "policy_decision"
    RISK_MISMATCH = "risk_mismatch"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_RESOLVED = "approval_resolved"
    ACTION_STARTED = "action_started"
    ACTION_SUCCEEDED = "action_succeeded"
    ACTION_FAILED = "action_failed"
    SIGNED_ON = "signed_on"
    INTERVENTION_REQUESTED = "intervention_requested"
    INTERVENTION_RESOLVED = "intervention_resolved"
    SNAPSHOT = "snapshot"
    TARGET_AUDIT = "target_audit"


class Event(BaseModel):
    seq: int
    at: datetime
    kind: str  # EventKind, or a driver's own kind (e.g. "step_started")
    step_id: str | None = None
    data: dict[str, Any] = {}


def new_run_id() -> str:
    return f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}"


def snapshot_digest(obs: Observation) -> str:
    return hashlib.sha256(obs.snapshot.encode()).hexdigest()[:16]


class RunLog:
    """One run's evidence directory:

        <root>/<run_id>/events.jsonl        append-only, one redacted event per line
        <root>/<run_id>/snapshots/NNNN-*    redacted tree + text, and a screenshot
        <root>/<run_id>/*.json              artifacts and results written by drivers
    """

    def __init__(self, root: Path, redactor: Redactor, run_id: str | None = None) -> None:
        self.run_id = run_id or new_run_id()
        self.dir = root / self.run_id
        self.dir.mkdir(parents=True, exist_ok=False)
        self.redactor = redactor
        self._seq = 0
        self._lock = threading.Lock()
        self._events = (self.dir / "events.jsonl").open("a", encoding="utf-8")

    def __enter__(self) -> "RunLog":
        return self

    def __exit__(
        self, et: type[BaseException] | None, e: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()

    def close(self) -> None:
        self._events.close()

    def emit(self, kind: str, step_id: str | None = None, /, **data: Any) -> Event:
        with self._lock:
            self._seq += 1
            event = Event(
                seq=self._seq,
                at=datetime.now(UTC),
                kind=kind,
                step_id=step_id,
                data=self.redactor.redact(_jsonable(data)),
            )
            self._events.write(event.model_dump_json() + "\n")
            self._events.flush()  # a crashed run still leaves its trail
            return event

    def snapshot(
        self,
        label: str,
        *,
        step_id: str | None = None,
        observation: Observation | None = None,
        screenshot: bytes | None = None,
    ) -> str:
        """Save what the engine saw; returns the path relative to the run directory."""
        with self._lock:
            n = self._seq + 1
        stem = Path("snapshots") / f"{n:04d}-{label}"
        (self.dir / "snapshots").mkdir(exist_ok=True)
        if observation is not None:
            body = (
                f"url: {observation.url}\ntitle: {observation.title}\n"
                f"frames: {observation.frame_urls}\n\n--- tree ---\n{observation.snapshot}\n"
                f"\n--- text ---\n{observation.text}\n"
            )
            (self.dir / stem.with_suffix(".txt")).write_text(self.redactor.scrub(body))
        if screenshot is not None:
            # Pixels can't be scrubbed; the surface masks sensitive inputs before capture.
            (self.dir / stem.with_suffix(".png")).write_bytes(screenshot)
        self.emit(EventKind.SNAPSHOT, step_id, label=label, path=str(stem),
                  screenshot=screenshot is not None)
        return str(stem)

    def write_json(self, name: str, data: Any) -> Path:
        path = self.dir / name
        path.write_text(json.dumps(self.redactor.redact(_jsonable(data)), indent=2) + "\n")
        return path


def _jsonable(data: Any) -> Any:
    """Plain JSON types, so redaction sees every string (pydantic models, enums, dates...)."""
    return json.loads(json.dumps(data, default=_default))


def _default(obj: Any) -> Any:
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if isinstance(obj, StrEnum):
        return obj.value
    return str(obj)
