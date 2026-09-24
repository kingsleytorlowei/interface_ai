"""Composition root: wires modules together and exposes the demo commands."""

import json
import os
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer
import yaml

from cua.control import ControlPort, InMemoryControl, OperatorDesk
from cua.discovery import ClaudePlanner, Goal, RecordingError, discover, prune_strategies
from cua.evidence import RunLog
from cua.operator import serve_console
from cua.policy import Mode, Policy, Redactor
from cua.replay import replay as run_replay
from cua.schema import (
    Capability,
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

app = typer.Typer(no_args_is_help=True)

CATALOG = Path("catalog")
EVIDENCE = Path("evidence")
BASE_URL = typer.Option("http://127.0.0.1:8001", help="Tenant base URL (allowlisted origin)")
HEADED = typer.Option(False, help="Show the browser window")
TENANT = typer.Option(None, help="Tenant whose overlay to apply (none: the base as recorded)")
PARAM_SETS = typer.Option(..., "--params", help="JSON inputs; pass twice, with different "
                                                "values, to prove the locators hold for both")
OPERATOR = typer.Option(
    "unattended", help="unattended: fail closed (reject approvals, abort handoffs); "
                       "console: a human decides in the web console (forces --headed)")


# The result kind is the contract; the exit code mirrors it for shell callers. 2 is a usage
# error (Typer's own convention), raised before anything runs.
EXIT_CODES = {"success": 0, "failure": 1, "business_outcome": 3, "aborted": 4}


def usage_error(message: str) -> typer.Exit:
    typer.echo(f"error: {message}", err=True)
    return typer.Exit(2)


@contextmanager
def store_errors() -> Iterator[None]:
    try:
        yield
    except StoreError as e:
        raise usage_error(str(e)) from e


def load_dotenv(path: Path = Path(".env")) -> None:
    """Minimal .env support: KEY=VALUE lines, never overriding the real environment."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#") and value:
            os.environ.setdefault(key, value.strip().strip('"').strip("'"))


@contextmanager
def open_session(store: Store, app_id: str, base_url: str, *, mode: Mode,
                 status: Status | None, control: ControlPort, headed: bool,
                 audit_targets: bool = False,
                 evidence: Path | None = None) -> Iterator[GuardedSession]:
    with ExitStack() as stack:
        surface = stack.enter_context(WebSurface.launch(headless=not headed))
        log = stack.enter_context(RunLog(evidence or EVIDENCE, Redactor()))
        yield stack.enter_context(GuardedSession.open(
            surface=surface, app=store.app(app_id),
            policy=Policy(store.policy(app_id), [base_url]), control=control, log=log,
            secrets=EnvSecrets(), env={"base_url": base_url}, mode=mode,
            capability_status=status, audit_targets=audit_targets))


def operator_control(mode: str) -> tuple[ControlPort, bool]:
    """(control port, whether the browser must be headed)."""
    if mode == "unattended":
        return InMemoryControl(), False
    if mode == "console":
        desk = OperatorDesk()
        url = serve_console(desk, EVIDENCE)
        typer.echo(f"operator console: {url} (hand-offs happen in the browser window)")
        return desk, True
    raise typer.BadParameter(f"unknown operator mode {mode!r}")


def verification_control() -> InMemoryControl:
    """Verifying a draft: its reversible steps were just performed during discovery, so they
    are approved; irreversible steps never are (verification must not commit anything)."""
    return InMemoryControl(approve=lambda r: r.risk is Risk.REVERSIBLE)


def parse_params(params: str) -> dict[str, Any]:
    try:
        inputs = json.loads(params)
    except json.JSONDecodeError as e:
        raise usage_error(f"--params is not valid JSON: {e}") from e
    if not isinstance(inputs, dict):
        raise usage_error("--params must be a JSON object")
    return inputs


def show(result: RunResult) -> None:
    typer.echo(result.model_dump_json(indent=2))


@app.command(name="discover")
def discover_command(
    goal_file: Path, base_url: str = BASE_URL, headed: bool = HEADED,
    operator: str = OPERATOR,
    model: str = typer.Option("claude-opus-5", help="Claude model id"),
) -> None:
    """Run LLM discovery on a goal, verify the draft by replaying it, and save it."""
    load_dotenv()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise usage_error("discovery needs ANTHROPIC_API_KEY (set it in .env); "
                          "replay and approve don't")
    store = Store(CATALOG)
    goal = Goal.model_validate(yaml.safe_load(goal_file.read_text()))
    control, needs_window = operator_control(operator)
    with open_session(store, goal.app, base_url, mode=Mode.DISCOVERY, status=None,
                      control=control, headed=headed or needs_window) as session:
        result = discover(goal, session, ClaudePlanner(model=model))
        typer.echo(f"discovery: {result.status} ({result.reason}) in {result.turns} turns; "
                   f"usage {result.usage}; evidence {session.log.dir}")
    for note in result.review_notes:
        typer.echo(f"  review: {note}")
    if result.capability is None:
        raise typer.Exit(1)

    capability = result.capability.model_copy(
        update={"version": store.next_draft_version(result.capability.id)})
    # Verify by replay, once per example set, auditing every strategy of every target.
    redactors = [session.log.redactor]
    runs: list[RunResult] = []
    audits: list[dict[str, list[bool]]] = []
    for params in goal.verification_params():
        with open_session(store, goal.app, base_url, mode=Mode.REPLAY, status=Status.DRAFT,
                          control=verification_control(), headed=headed,
                          audit_targets=True) as session:
            runs.append(run_replay(capability, params, session))
        redactors.append(session.log.redactor)
        audits.append(session.target_audits)
        typer.echo(f"verification {len(runs)}: {runs[-1].kind}; evidence "
                   f"{runs[-1].evidence_ref}")
        if runs[-1].kind != "success":
            break
    if len(runs) == 1 and runs[0].kind == "success":
        typer.echo("  review: the goal has no alt_example inputs, so fallback strategies are "
                   "unproven beyond one input set")
    kind = next((r.kind for r in runs if r.kind != "success"), "success")
    if kind == "success":
        try:
            capability, notes = prune_strategies(capability, audits)
        except RecordingError as e:
            kind, notes = "failure", [str(e)]
        for note in notes:
            typer.echo(f"  review: {note}")
    # The store keeps only what the approval gate needs; the full (redacted) results, with
    # their outputs, live in the evidence directories they point to. It refuses an artifact
    # containing any value this run knows to be sensitive.
    with store_errors():
        path = store.save(capability, {"kind": kind, "runs": [
            {"kind": r.kind, "run_id": r.run_id, "evidence_ref": r.evidence_ref}
            for r in runs]}, sensitive=redactors)
    typer.echo(f"draft saved: {path}")
    if kind != "success":
        show(runs[-1])
        raise typer.Exit(1)


@app.command()
def replay(
    capability_id: str, params: str = typer.Option("{}", help="JSON object of inputs"),
    version: str | None = None, base_url: str = BASE_URL, headed: bool = HEADED,
    tenant: str | None = TENANT, operator: str = OPERATOR,
) -> None:
    """Replay a saved capability deterministically with JSON params (by default its latest
    approved version, with the tenant's approved overlay; pass --version to replay drafts)."""
    load_dotenv()
    store = Store(CATALOG)
    with store_errors():
        capability: Capability = store.load(
            capability_id, version, status=None if version else Status.APPROVED, tenant=tenant)
    inputs = parse_params(params)
    if operator == "unattended" and (reasons := clearance(
            capability, store.stability(capability.id, capability.version),
            store.policy(capability.app.app_id).unattended, datetime.now(UTC))):
        raise usage_error("not cleared for unattended replay: " + "; ".join(reasons)
                          + ". Run it with --operator console, where a person can step in.")
    control, needs_window = operator_control(operator)
    with open_session(store, capability.app.app_id, base_url, mode=Mode.REPLAY,
                      status=capability.status, control=control,
                      headed=headed or needs_window) as session:
        result = run_replay(capability, inputs, session)
    show(result)
    raise typer.Exit(EXIT_CODES[result.kind])


@app.command()
def stability(
    capability_id: str, tenant: str | None = TENANT,
    runs: int = typer.Option(10, min=1, help="How many replays"),
    params: list[str] = PARAM_SETS, base_url: str = BASE_URL, headed: bool = HEADED,
) -> None:
    """Measure how reliably the approved capability (with the tenant's overlay) replays:
    N unattended runs, cycling through the --params sets. Saves the report that clears it
    for unattended replay, or says why not (exit 1)."""
    load_dotenv()
    store = Store(CATALOG)
    with store_errors():
        capability = store.load(capability_id, status=Status.APPROVED, tenant=tenant)
    if capability.risk is Risk.IRREVERSIBLE:
        raise usage_error("it has irreversible steps: every run needs a person's approval, so "
                          "it is never unattended, and measuring would repeat real commits")
    param_sets = [parse_params(p) for p in params]
    measured_at = datetime.now(UTC)
    evidence = (EVIDENCE / "stability" /
                f"{capability.id}@{capability.version}-{measured_at:%Y%m%dT%H%M%SZ}")
    records: list[StabilityRun] = []
    for i in range(runs):
        with open_session(store, capability.app.app_id, base_url, mode=Mode.REPLAY,
                          status=capability.status, control=InMemoryControl(),
                          headed=headed, evidence=evidence) as session:
            result = run_replay(capability, param_sets[i % len(param_sets)], session)
        records.append(StabilityRun(
            run_id=result.run_id, evidence_ref=result.evidence_ref,
            params_set=i % len(param_sets), kind=result.kind,
            category=getattr(result, "category", None), step_id=getattr(result, "step_id", None),
            drift_targets=sorted({d.target for d in result.drift}),
            recoveries=[r.recovery for r in result.recoveries],
            duration_s=round((result.finished_at - result.started_at).total_seconds(), 2)))
        typer.echo(f"run {i + 1}/{runs}: {result.kind}")
    report = StabilityReport.from_runs(capability.id, capability.version, tenant, measured_at,
                                       records)
    path = store.save_stability(report)
    typer.echo(f"{report.verdict}: {report.success_rate:.0%} success over {report.runs_total} "
               f"runs, {report.drift_runs} with drift, {report.recovery_runs} with recoveries, "
               f"p50 {report.duration_p50_s}s; saved {path}")
    reasons = clearance(capability, report, store.policy(capability.app.app_id).unattended,
                        datetime.now(UTC))
    if reasons:
        typer.echo("not cleared for unattended replay: " + "; ".join(reasons))
        raise typer.Exit(1)
    typer.echo("cleared for unattended replay")


@app.command(name="verify-overlay")
def verify_overlay(
    capability_id: str,
    tenant: str = typer.Option(..., help="Tenant the overlay is for"),
    version: str | None = typer.Option(None, help="Base version (default: latest approved)"),
    params: list[str] = PARAM_SETS,
    base_url: str = BASE_URL, headed: bool = HEADED,
) -> None:
    """Verify a draft tenant overlay by replaying the base with it on that tenant, once per
    --params set, keeping only the overlay strategies that held in every run."""
    load_dotenv()
    store = Store(CATALOG)
    with store_errors():
        base = store.load(capability_id, version, status=Status.APPROVED)
        overlay = store.overlay(tenant, capability_id, base.version)
        if overlay is None:
            raise StoreError(f"no {tenant} overlay for {capability_id}@{base.version}")
        if overlay.status is not Status.DRAFT:
            raise StoreError(f"the {tenant} overlay is {overlay.status}; nothing to verify")
        capability = apply_overlay(base, overlay)
    param_sets = [parse_params(p) for p in params]
    redactors: list[Redactor] = []
    runs: list[RunResult] = []
    audits: list[dict[str, list[bool]]] = []
    for inputs in param_sets:
        with open_session(store, base.app.app_id, base_url, mode=Mode.REPLAY,
                          status=Status.DRAFT, control=verification_control(), headed=headed,
                          audit_targets=True) as session:
            runs.append(run_replay(capability, inputs, session))
        redactors.append(session.log.redactor)
        audits.append(session.target_audits)
        typer.echo(f"verification {len(runs)}: {runs[-1].kind}; evidence "
                   f"{runs[-1].evidence_ref}")
        if drift := [d.target for d in runs[-1].drift if d.target in overlay.targets]:
            typer.echo(f"  review: overlay targets resolved by a fallback: {drift}")
        if runs[-1].kind != "success":
            break
    if len(runs) == 1 and runs[0].kind == "success":
        typer.echo("  review: one --params set only, so fallback strategies are unproven "
                   "beyond one input set")
    kind = next((r.kind for r in runs if r.kind != "success"), "success")
    if kind == "success":
        try:
            pruned, notes = prune_strategies(capability, audits,
                                            only=overlay.targets.keys())
        except RecordingError as e:
            kind, notes = "failure", [str(e)]
        else:
            overlay = overlay.model_copy(update={
                "targets": {name: pruned.targets[name] for name in overlay.targets}})
        for note in notes:
            typer.echo(f"  review: {note}")
    with store_errors():
        path = store.save_overlay(overlay, {"kind": kind, "runs": [
            {"kind": r.kind, "run_id": r.run_id, "evidence_ref": r.evidence_ref}
            for r in runs]}, sensitive=redactors)
    typer.echo(f"overlay saved: {path}")
    if kind != "success":
        show(runs[-1])
        raise typer.Exit(1)


@app.command()
def approve(
    capability_id: str, version: str, reviewer: str = typer.Option(..., help="Who signs off"),
    read_only: str = typer.Option("", help="Comma-separated reversible steps that only query"),
    tenant: str | None = typer.Option(None, help="Approve this tenant's overlay for the base "
                                                 "version instead of the base"),
) -> None:
    """Approve a verified draft (optionally lowering query-only steps to read_only), or with
    --tenant, a verified tenant overlay."""
    steps = [s.strip() for s in read_only.split(",") if s.strip()]
    if tenant is not None:
        if steps:
            raise usage_error("--read-only applies to capabilities; an overlay only moves "
                              "targets, so it has no risk to lower")
        with store_errors():
            store = Store(CATALOG)
            store.approve_overlay(tenant, capability_id, version, reviewer)
        typer.echo(f"approved the {tenant} overlay for {capability_id}@{version} by {reviewer}")
        return
    with store_errors():
        approved = Store(CATALOG).approve(capability_id, version, reviewer,
                                          read_only_steps=steps)
    typer.echo(f"approved {approved.id}@{approved.version} (risk {approved.risk}) "
               f"by {reviewer}")


if __name__ == "__main__":
    app()
