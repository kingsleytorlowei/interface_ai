"""Composition root: wires modules together and exposes the demo commands."""

import json
import os
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
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
from cua.schema import Capability, Risk, RunResult, Status
from cua.secrets import EnvSecrets
from cua.session import GuardedSession
from cua.store import Store, StoreError
from cua.surface.web import WebSurface

app = typer.Typer(no_args_is_help=True)

CATALOG = Path("catalog")
EVIDENCE = Path("evidence")
BASE_URL = typer.Option("http://127.0.0.1:8001", help="Tenant base URL (allowlisted origin)")
HEADED = typer.Option(False, help="Show the browser window")
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
                 audit_targets: bool = False) -> Iterator[GuardedSession]:
    with ExitStack() as stack:
        surface = stack.enter_context(WebSurface.launch(headless=not headed))
        log = stack.enter_context(RunLog(EVIDENCE, Redactor()))
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
    operator: str = OPERATOR,
) -> None:
    """Replay a saved capability deterministically with JSON params (by default its latest
    approved version; pass --version to replay a draft)."""
    load_dotenv()
    store = Store(CATALOG)
    with store_errors():
        capability: Capability = store.load(
            capability_id, version, status=None if version else Status.APPROVED)
    try:
        inputs: dict[str, Any] = json.loads(params)
    except json.JSONDecodeError as e:
        raise usage_error(f"--params is not valid JSON: {e}") from e
    if not isinstance(inputs, dict):
        raise usage_error("--params must be a JSON object")
    control, needs_window = operator_control(operator)
    with open_session(store, capability.app.app_id, base_url, mode=Mode.REPLAY,
                      status=capability.status, control=control,
                      headed=headed or needs_window) as session:
        result = run_replay(capability, inputs, session)
    show(result)
    raise typer.Exit(EXIT_CODES[result.kind])


@app.command()
def approve(
    capability_id: str, version: str, reviewer: str = typer.Option(..., help="Who signs off"),
    read_only: str = typer.Option("", help="Comma-separated reversible steps that only query"),
) -> None:
    """Approve a verified draft (optionally lowering query-only steps to read_only)."""
    steps = [s.strip() for s in read_only.split(",") if s.strip()]
    with store_errors():
        approved = Store(CATALOG).approve(capability_id, version, reviewer,
                                          read_only_steps=steps)
    typer.echo(f"approved {approved.id}@{approved.version} (risk {approved.risk}) "
               f"by {reviewer}")


if __name__ == "__main__":
    app()
