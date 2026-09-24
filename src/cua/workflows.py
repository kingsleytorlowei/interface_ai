"""What people and programs do with the system, independent of how they ask: discover a goal
(and verify the draft), approve, replay, measure stability, verify a tenant overlay.

The CLI and the operator workbench are both thin front ends over these functions, so a run
started from either is the same run. Each takes a `Workspace` (where the catalog and
evidence live, which tenant URL to act on) and reports progress as plain sentences through a
callback; refusals that are the caller's to fix are `WorkflowError`s (or `StoreError`s).
"""

from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cua.control import ControlPort, InMemoryControl
from cua.discovery import DiscoveryResult, Goal, Planner, RecordingError, discover, prune_strategies
from cua.evidence import RunLog
from cua.policy import Mode, Policy, Redactor
from cua.replay import replay as run_replay
from cua.schema import (
    Capability,
    CapabilityOverlay,
    Risk,
    RunResult,
    StabilityReport,
    StabilityRun,
    Status,
    apply_overlay,
)
from cua.secrets import EnvSecrets
from cua.session import GuardedSession
from cua.stability import clearance
from cua.store import Store, StoreError
from cua.surface.web import WebSurface

Progress = Callable[[str], None]


def quiet(_: str) -> None:
    pass


class WorkflowError(Exception):
    """A request that can't be carried out as asked; nothing ran."""


@dataclass(frozen=True)
class Workspace:
    store: Store
    evidence: Path
    base_url: str  # the tenant's URL: its origin is the one the policy allows
    headed: bool = False


@contextmanager
def open_session(ws: Workspace, app_id: str, *, mode: Mode, status: Status | None,
                 control: ControlPort, headed: bool = False, audit_targets: bool = False,
                 evidence: Path | None = None) -> Iterator[GuardedSession]:
    """A fresh browser and evidence directory for one run, behind the guarded session."""
    with ExitStack() as stack:
        surface = stack.enter_context(WebSurface.launch(headless=not (headed or ws.headed)))
        log = stack.enter_context(RunLog(evidence or ws.evidence, Redactor()))
        yield stack.enter_context(GuardedSession.open(
            surface=surface, app=ws.store.app(app_id),
            policy=Policy(ws.store.policy(app_id), [ws.base_url]), control=control, log=log,
            secrets=EnvSecrets(), env={"base_url": ws.base_url}, mode=mode,
            capability_status=status, audit_targets=audit_targets))


def verification_control() -> InMemoryControl:
    """Verifying a draft: its reversible steps were just performed during discovery, so they
    are approved; irreversible steps never are (verification must not commit anything)."""
    return InMemoryControl(approve=lambda r: r.risk is Risk.REVERSIBLE)


def _summary(runs: list[RunResult]) -> dict[str, Any]:
    """What the store keeps for the approval gate: kinds and pointers, never outputs (the
    full redacted results live in the evidence directories)."""
    kind = next((r.kind for r in runs if r.kind != "success"), "success")
    return {"kind": kind, "runs": [
        {"kind": r.kind, "run_id": r.run_id, "evidence_ref": r.evidence_ref} for r in runs]}


# --- discover -------------------------------------------------------------------------------


@dataclass
class DiscoveryOutcome:
    discovery: DiscoveryResult
    evidence_ref: str
    draft: Capability | None = None  # the saved draft, if discovery recorded a flow
    draft_path: Path | None = None
    verification: list[RunResult] = field(default_factory=list)
    # "success" makes the draft approvable; anything else is why it isn't
    verification_kind: str | None = None
    review_notes: list[str] = field(default_factory=list)

    @property
    def approvable(self) -> bool:
        return self.verification_kind == "success"


def discover_goal(ws: Workspace, goal: Goal, planner: Planner, *, control: ControlPort,
                  headed: bool = False, progress: Progress = quiet) -> DiscoveryOutcome:
    """Discover the goal with the LLM, then verify the draft by replaying it without the LLM
    once per example set, auditing every strategy of every target and dropping those that
    didn't hold in every run, and save it as a draft (verified or not)."""
    store = ws.store
    with open_session(ws, goal.app, mode=Mode.DISCOVERY, status=None, control=control,
                      headed=headed) as session:
        found = discover(goal, session, planner)
    outcome = DiscoveryOutcome(found, str(session.log.dir), review_notes=list(found.review_notes))
    progress(f"discovery: {found.status} ({found.reason}) in {found.turns} turns; "
             f"usage {found.usage}; evidence {session.log.dir}")
    for note in found.review_notes:
        progress(f"  review: {note}")
    if found.capability is None:
        return outcome

    capability = found.capability.model_copy(
        update={"version": store.next_draft_version(found.capability.id)})
    redactors = [session.log.redactor]
    audits: list[dict[str, list[bool]]] = []
    for params in goal.verification_params():
        with open_session(ws, goal.app, mode=Mode.REPLAY, status=Status.DRAFT,
                          control=verification_control(), audit_targets=True) as session:
            outcome.verification.append(run_replay(capability, params, session))
        redactors.append(session.log.redactor)
        audits.append(session.target_audits)
        result = outcome.verification[-1]
        progress(f"verification {len(outcome.verification)}: {result.kind}; evidence "
                 f"{result.evidence_ref}")
        if result.kind != "success":
            break
    notes: list[str] = []
    summary = _summary(outcome.verification)
    if len(outcome.verification) == 1 and summary["kind"] == "success":
        notes.append("the goal has no alt_example inputs, so fallback strategies are unproven "
                     "beyond one input set")
    if summary["kind"] == "success":
        try:
            capability, pruned = prune_strategies(capability, audits)
            notes += pruned
        except RecordingError as e:
            summary["kind"] = "failure"
            notes.append(str(e))
    for note in notes:
        progress(f"  review: {note}")
    outcome.review_notes += notes
    # Refuses an artifact containing any value this run knows to be sensitive.
    outcome.draft_path = store.save(capability, summary, sensitive=redactors)
    outcome.draft, outcome.verification_kind = capability, summary["kind"]
    progress(f"draft saved: {outcome.draft_path}")
    return outcome


# --- approve --------------------------------------------------------------------------------


def approve(ws: Workspace, capability_id: str, version: str, reviewer: str, *,
            read_only_steps: list[str] | None = None) -> Capability:
    if not reviewer.strip():
        raise WorkflowError("approval needs the reviewer's name")
    return ws.store.approve(capability_id, version, reviewer, read_only_steps=read_only_steps)


def approve_overlay(ws: Workspace, tenant: str, capability_id: str, base_version: str,
                    reviewer: str) -> CapabilityOverlay:
    if not reviewer.strip():
        raise WorkflowError("approval needs the reviewer's name")
    return ws.store.approve_overlay(tenant, capability_id, base_version, reviewer)


# --- replay ---------------------------------------------------------------------------------


def load_for_replay(ws: Workspace, capability_id: str, *, version: str | None = None,
                    tenant: str | None = None) -> Capability:
    """The latest approved version (with the tenant's approved overlay), or a named one."""
    return ws.store.load(capability_id, version,
                         status=None if version else Status.APPROVED, tenant=tenant)


def unattended_clearance(ws: Workspace, capability: Capability) -> list[str]:
    """Why this capability may not run with nobody watching; empty means it may."""
    return clearance(capability, ws.store.stability(capability.id, capability.version),
                     ws.store.policy(capability.app.app_id).unattended, datetime.now(UTC))


def replay_capability(ws: Workspace, capability: Capability, inputs: Mapping[str, Any], *,
                      control: ControlPort, attended: bool, headed: bool = False) -> RunResult:
    """Replay without the LLM. Unattended runs need the capability to be cleared; attended
    ones (a person can answer approvals and handoffs) run on approval alone."""
    if not attended and (reasons := unattended_clearance(ws, capability)):
        raise WorkflowError("not cleared for unattended replay: " + "; ".join(reasons))
    with open_session(ws, capability.app.app_id, mode=Mode.REPLAY, status=capability.status,
                      control=control, headed=headed) as session:
        return run_replay(capability, inputs, session)


# --- stability ------------------------------------------------------------------------------


@dataclass
class StabilityOutcome:
    report: StabilityReport
    path: Path
    reasons: list[str]  # why it isn't cleared; empty means cleared

    @property
    def cleared(self) -> bool:
        return not self.reasons


def measure_stability(ws: Workspace, capability: Capability,
                      param_sets: list[dict[str, Any]], *, runs: int = 10,
                      progress: Progress = quiet) -> StabilityOutcome:
    """Replay an approved capability `runs` times unattended, cycling through the input
    sets, and save the report that clears it for unattended replay (or says why not)."""
    if capability.status is not Status.APPROVED:
        raise WorkflowError(f"{capability.id}@{capability.version} is {capability.status}; "
                            "only approved capabilities are measured")
    if capability.risk is Risk.IRREVERSIBLE:
        raise WorkflowError("it has irreversible steps: every run needs a person's approval, "
                            "so it is never unattended, and measuring would repeat real "
                            "commits")
    if not param_sets:
        raise WorkflowError("measuring needs at least one set of inputs")
    measured_at = datetime.now(UTC)
    evidence = (ws.evidence / "stability" /
                f"{capability.id}@{capability.version}-{measured_at:%Y%m%dT%H%M%SZ}")
    records: list[StabilityRun] = []
    for i in range(runs):
        with open_session(ws, capability.app.app_id, mode=Mode.REPLAY,
                          status=capability.status, control=InMemoryControl(),
                          evidence=evidence) as session:
            result = run_replay(capability, param_sets[i % len(param_sets)], session)
        records.append(StabilityRun(
            run_id=result.run_id, evidence_ref=result.evidence_ref,
            params_set=i % len(param_sets), kind=result.kind,
            category=getattr(result, "category", None), step_id=getattr(result, "step_id", None),
            drift_targets=sorted({d.target for d in result.drift}),
            recoveries=[r.recovery for r in result.recoveries],
            duration_s=round((result.finished_at - result.started_at).total_seconds(), 2)))
        progress(f"run {i + 1}/{runs}: {result.kind}")
    tenant = capability.version.partition("+")[2] or None
    report = StabilityReport.from_runs(capability.id, capability.version, tenant, measured_at,
                                       records)
    path = ws.store.save_stability(report)
    progress(f"{report.verdict}: {report.success_rate:.0%} success over {report.runs_total} "
             f"runs, {report.drift_runs} with drift, {report.recovery_runs} with recoveries, "
             f"p50 {report.duration_p50_s}s; saved {path}")
    return StabilityOutcome(report, path, unattended_clearance(ws, capability))


# --- tenant overlays ------------------------------------------------------------------------


@dataclass
class OverlayOutcome:
    overlay: CapabilityOverlay
    path: Path
    verification: list[RunResult]
    verification_kind: str
    review_notes: list[str]

    @property
    def approvable(self) -> bool:
        return self.verification_kind == "success"


def verify_overlay(ws: Workspace, capability_id: str, tenant: str,
                   param_sets: list[dict[str, Any]], *, version: str | None = None,
                   progress: Progress = quiet) -> OverlayOutcome:
    """Verify a draft tenant overlay by replaying the base with it on that tenant, once per
    input set, keeping only the overlay strategies that held in every run."""
    store = ws.store
    base = store.load(capability_id, version, status=Status.APPROVED)
    overlay = store.overlay(tenant, capability_id, base.version)
    if overlay is None:
        raise StoreError(f"no {tenant} overlay for {capability_id}@{base.version}")
    if overlay.status is not Status.DRAFT:
        raise StoreError(f"the {tenant} overlay is {overlay.status}; nothing to verify")
    capability = apply_overlay(base, overlay)
    redactors: list[Redactor] = []
    runs: list[RunResult] = []
    audits: list[dict[str, list[bool]]] = []
    notes: list[str] = []
    for inputs in param_sets:
        with open_session(ws, base.app.app_id, mode=Mode.REPLAY, status=Status.DRAFT,
                          control=verification_control(), audit_targets=True) as session:
            runs.append(run_replay(capability, inputs, session))
        redactors.append(session.log.redactor)
        audits.append(session.target_audits)
        progress(f"verification {len(runs)}: {runs[-1].kind}; evidence "
                 f"{runs[-1].evidence_ref}")
        if drift := [d.target for d in runs[-1].drift if d.target in overlay.targets]:
            notes.append(f"overlay targets resolved by a fallback: {drift}")
        if runs[-1].kind != "success":
            break
    if len(runs) == 1 and runs[0].kind == "success":
        notes.append("one input set only, so fallback strategies are unproven beyond it")
    summary = _summary(runs)
    if summary["kind"] == "success":
        try:
            pruned, dropped = prune_strategies(capability, audits,
                                               only=overlay.targets.keys())
        except RecordingError as e:
            summary["kind"] = "failure"
            notes.append(str(e))
        else:
            notes += dropped
            overlay = overlay.model_copy(update={
                "targets": {name: pruned.targets[name] for name in overlay.targets}})
    for note in notes:
        progress(f"  review: {note}")
    path = store.save_overlay(overlay, summary, sensitive=redactors)
    progress(f"overlay saved: {path}")
    return OverlayOutcome(overlay, path, runs, summary["kind"], notes)
