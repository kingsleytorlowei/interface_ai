"""Composition root: wires modules together and exposes the demo commands."""

import json
import os
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any

import typer
import yaml

from cua.control import ApprovalRequest, ControlPort, InMemoryControl
from cua.discovery import ClaudePlanner, Goal, discover
from cua.evidence import RunLog
from cua.policy import Mode, Policy, Redactor
from cua.replay import replay as run_replay
from cua.schema import Capability, Risk, RunResult, Status
from cua.secrets import EnvSecrets
from cua.session import GuardedSession
from cua.store import Store
from cua.surface.web import WebSurface

app = typer.Typer(no_args_is_help=True)

CATALOG = Path("catalog")
EVIDENCE = Path("evidence")
BASE_URL = typer.Option("http://127.0.0.1:8001", help="Tenant base URL (allowlisted origin)")
HEADED = typer.Option(False, help="Show the browser window")


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
                 status: Status | None, control: ControlPort, headed: bool
                 ) -> Iterator[GuardedSession]:
    with ExitStack() as stack:
        surface = stack.enter_context(WebSurface.launch(headless=not headed))
        log = stack.enter_context(RunLog(EVIDENCE, Redactor()))
        yield stack.enter_context(GuardedSession.open(
            surface=surface, app=store.app(app_id),
            policy=Policy(store.policy(app_id), [base_url]), control=control, log=log,
            secrets=EnvSecrets(), env={"base_url": base_url}, mode=mode,
            capability_status=status))


def verification_control() -> InMemoryControl:
    """Verifying a draft: its reversible steps were just performed during discovery, so they
    are approved; irreversible steps never are (verification must not commit anything)."""
    return InMemoryControl(approve=lambda r: r.risk is Risk.REVERSIBLE)


def show(result: RunResult) -> None:
    typer.echo(result.model_dump_json(indent=2))


@app.command(name="discover")
def discover_command(
    goal_file: Path, base_url: str = BASE_URL, headed: bool = HEADED,
    model: str = typer.Option("claude-opus-5", help="Claude model id"),
) -> None:
    """Run LLM discovery on a goal, verify the draft by replaying it, and save it."""
    load_dotenv()
    store = Store(CATALOG)
    goal = Goal.model_validate(yaml.safe_load(goal_file.read_text()))
    with open_session(store, goal.app, base_url, mode=Mode.DISCOVERY, status=None,
                      control=InMemoryControl(), headed=headed) as session:
        result = discover(goal, session, ClaudePlanner(model=model))
        typer.echo(f"discovery: {result.status} ({result.reason}) in {result.turns} turns; "
                   f"usage {result.usage}; evidence {session.log.dir}")
    for note in result.review_notes:
        typer.echo(f"  review: {note}")
    if result.capability is None:
        raise typer.Exit(1)

    params = goal.examples()
    with open_session(store, goal.app, base_url, mode=Mode.REPLAY, status=Status.DRAFT,
                      control=verification_control(), headed=headed) as session:
        verification = run_replay(result.capability, params, session)
    path = store.save(result.capability, verification.model_dump(mode="json"))
    typer.echo(f"verification: {verification.kind}; evidence {verification.evidence_ref}")
    typer.echo(f"draft saved: {path}")
    if verification.kind != "success":
        show(verification)
        raise typer.Exit(1)


@app.command()
def replay(
    capability_id: str, params: str = typer.Option("{}", help="JSON object of inputs"),
    version: str | None = None, base_url: str = BASE_URL, headed: bool = HEADED,
    approve: bool = typer.Option(False, help="Approve steps that need a human (unattended)"),
) -> None:
    """Replay a saved capability deterministically with JSON params."""
    load_dotenv()
    store = Store(CATALOG)
    capability: Capability = store.load(capability_id, version)

    def decide(request: ApprovalRequest) -> bool:
        typer.echo(f"approval requested: {request.action} ({request.risk}): "
                   f"{'approved' if approve else 'rejected'}")
        return approve

    inputs: dict[str, Any] = json.loads(params)
    with open_session(store, capability.app.app_id, base_url, mode=Mode.REPLAY,
                      status=capability.status, control=InMemoryControl(approve=decide),
                      headed=headed) as session:
        result = run_replay(capability, inputs, session)
    show(result)
    if result.kind != "success":
        raise typer.Exit(1)


@app.command()
def approve(
    capability_id: str, version: str, reviewer: str = typer.Option(..., help="Who signs off"),
    read_only: str = typer.Option("", help="Comma-separated reversible steps that only query"),
) -> None:
    """Approve a verified draft (optionally lowering query-only steps to read_only)."""
    steps = [s.strip() for s in read_only.split(",") if s.strip()]
    approved = Store(CATALOG).approve(capability_id, version, reviewer, read_only_steps=steps)
    typer.echo(f"approved {approved.id}@{approved.version} (risk {approved.risk}) "
               f"by {reviewer}")


if __name__ == "__main__":
    app()
