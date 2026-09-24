"""Background jobs for the workbench: one at a time, because a process drives one live
browser. A job is a workflow call plus the progress sentences it reports; the page that
started it polls it until it's done.
"""

import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from cua.store import StoreError
from cua.workflows import Progress, WorkflowError

JobStatus = Literal["running", "done", "refused", "crashed"]


class Busy(Exception):
    """Another job is running; the live browser is taken."""


@dataclass
class Job:
    id: str
    kind: str  # "run", "stability", ...
    title: str
    subject: str | None = None  # the capability id it concerns, for links back
    status: JobStatus = "running"
    lines: list[str] = field(default_factory=list)
    result: Any = None
    error: str | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None

    @property
    def running(self) -> bool:
        return self.status == "running"


class JobRunner:
    def __init__(self, keep: int = 50) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._current: Job | None = None
        self._keep = keep

    def submit(self, kind: str, title: str, work: Callable[[Progress], Any], *,
               subject: str | None = None) -> Job:
        """Start `work(progress)` on a worker thread, or raise `Busy`."""
        with self._lock:
            if self._current is not None:
                raise Busy(f"busy: {self._current.title}")
            job = Job(id=secrets.token_hex(4), kind=kind, title=title, subject=subject)
            self._current = job
            self._jobs[job.id] = job
            for old in list(self._jobs)[:-self._keep]:
                del self._jobs[old]
        threading.Thread(target=self._run, args=(job, work), daemon=True,
                         name=f"job-{job.id}").start()
        return job

    def _run(self, job: Job, work: Callable[[Progress], Any]) -> None:
        try:
            job.result = work(job.lines.append)
            job.status = "done"
        except (WorkflowError, StoreError) as e:
            job.error, job.status = str(e), "refused"
        except Exception as e:  # shown to the operator; the evidence has the details
            job.error, job.status = f"{type(e).__name__}: {e}", "crashed"
        finally:
            job.finished_at = datetime.now(UTC)
            with self._lock:
                self._current = None

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def current(self) -> Job | None:
        return self._current

    def recent(self) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.started_at, reverse=True)

    def wait(self, job_id: str, timeout_s: float = 60) -> Job:
        """For tests and scripts: block until the job has finished."""
        deadline = datetime.now(UTC).timestamp() + timeout_s
        while (job := self._jobs[job_id]).running:
            if datetime.now(UTC).timestamp() > deadline:
                raise TimeoutError(f"job {job_id} still running")
            threading.Event().wait(0.02)
        return job
