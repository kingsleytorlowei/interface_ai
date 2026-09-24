"""Composition root for engineers and scripts: a thin command line over `cua.workflows` (the
operator workbench is the other front end over the same functions)."""

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import typer
import yaml

from cua import workflows
from cua.control import ControlPort, InMemoryControl, OperatorDesk
from cua.discovery import ClaudePlanner, Goal
from cua.operator import serve_console
from cua.schema import RunResult
from cua.store import Store, StoreError
from cua.workflows import WorkflowError, Workspace

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
def refusals() -> Iterator[None]:
    """Requests the caller has to fix are one line and exit 2, never a traceback."""
    try:
        yield
    except (StoreError, WorkflowError) as e:
        raise usage_error(str(e)) from e


def load_dotenv(path: Path = Path(".env")) -> None:
    """Minimal .env support: KEY=VALUE lines, never overriding the real environment."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#") and value:
            os.environ.setdefault(key, value.strip().strip('"').strip("'"))


def workspace(base_url: str = "http://127.0.0.1:8001", headed: bool = False) -> Workspace:
    return Workspace(Store(CATALOG), EVIDENCE, base_url, headed)


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
    goal = Goal.model_validate(yaml.safe_load(goal_file.read_text()))
    control, needs_window = operator_control(operator)
    with refusals():
        outcome = workflows.discover_goal(
            workspace(base_url, headed), goal, ClaudePlanner(model=model), control=control,
            headed=needs_window, progress=typer.echo)
    if outcome.draft is None:
        raise typer.Exit(1)
    if not outcome.approvable:
        if outcome.verification:
            show(outcome.verification[-1])
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
    ws = workspace(base_url, headed)
    with refusals():
        capability = workflows.load_for_replay(ws, capability_id, version=version,
                                               tenant=tenant)
    inputs = parse_params(params)
    attended = operator != "unattended"
    if not attended and (reasons := workflows.unattended_clearance(ws, capability)):
        raise usage_error("not cleared for unattended replay: " + "; ".join(reasons)
                          + ". Run it with --operator console, where a person can step in.")
    control, needs_window = operator_control(operator)
    with refusals():
        result = workflows.replay_capability(ws, capability, inputs, control=control,
                                             attended=attended, headed=needs_window)
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
    ws = workspace(base_url, headed)
    with refusals():
        capability = workflows.load_for_replay(ws, capability_id, tenant=tenant)
    param_sets = [parse_params(p) for p in params]
    with refusals():
        outcome = workflows.measure_stability(ws, capability, param_sets, runs=runs,
                                              progress=typer.echo)
    if not outcome.cleared:
        typer.echo("not cleared for unattended replay: " + "; ".join(outcome.reasons))
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
    param_sets = [parse_params(p) for p in params]
    with refusals():
        outcome = workflows.verify_overlay(workspace(base_url, headed), capability_id, tenant,
                                           param_sets, version=version, progress=typer.echo)
    if not outcome.approvable:
        show(outcome.verification[-1])
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
    ws = workspace()
    if tenant is not None:
        if steps:
            raise usage_error("--read-only applies to capabilities; an overlay only moves "
                              "targets, so it has no risk to lower")
        with refusals():
            workflows.approve_overlay(ws, tenant, capability_id, version, reviewer)
        typer.echo(f"approved the {tenant} overlay for {capability_id}@{version} by {reviewer}")
        return
    with refusals():
        approved = workflows.approve(ws, capability_id, version, reviewer,
                                     read_only_steps=steps)
    typer.echo(f"approved {approved.id}@{approved.version} (risk {approved.risk}) "
               f"by {reviewer}")


TENANT_URLS = typer.Option(None, "--tenant", help="A tenant with overlays, as NAME=URL; "
                                                 "repeat for several")


@app.command()
def console(
    base_url: str = BASE_URL, tenant_urls: list[str] | None = TENANT_URLS,
    port: int = typer.Option(8765, help="Port for the workbench"),
    headless: bool = typer.Option(False, help="Hide the automation's browser window (a "
                                              "handoff then has nowhere to happen)"),
) -> None:
    """Open the operator workbench: review and approve automations, run them, and answer
    what they ask for, in the browser."""
    import uvicorn

    from cua.operator.workbench import WorkbenchConfig, create_workbench

    load_dotenv()
    tenants: dict[str, str] = {}
    for item in tenant_urls or []:
        name, sep, url = item.partition("=")
        if not sep or not name or not url:
            raise usage_error(f"--tenant takes NAME=URL, not {item!r}")
        tenants[name] = url
    config = WorkbenchConfig(Store(CATALOG), EVIDENCE, base_url, tenants, headed=not headless)
    typer.echo(f"operator workbench: http://127.0.0.1:{port}")
    uvicorn.run(create_workbench(config), host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    app()
