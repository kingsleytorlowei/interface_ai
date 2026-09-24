"""The CLI contract: which version replay picks, exit codes per result kind, and one-line
errors instead of tracebacks. The session and the replay itself are faked; they have their
own tests."""

import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter
from typer.testing import CliRunner

from conftest import CATALOG, load_capability
from cua import cli, workflows
from cua.control import InMemoryControl
from cua.schema import Capability, RunResult
from cua.store import Store
from test_overlay import overlay
from test_store import draft

runner = CliRunner()
CAP = "corebank.member.lookup_balance"
NOW = datetime.now(UTC)

RESULTS: dict[str, dict[str, Any]] = {
    "success": {"outputs": {}},
    "business_outcome": {"code": "no_results", "message": "no such member"},
    "failure": {"category": "app_error", "step_id": "search", "expected": "detail",
                "observed": "error page", "retryable": True, "message": "app error"},
    "aborted": {"step_id": "search", "reason": "approval rejected"},
}


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Store:
    shutil.copytree(CATALOG / "corebank", tmp_path / "corebank",
                    ignore=shutil.ignore_patterns("capabilities", "tenants"))
    monkeypatch.setattr(cli, "CATALOG", tmp_path)
    return Store(tmp_path)


@pytest.fixture
def replayed(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Fakes the session and the replay; records which capability was replayed and returns
    the result kind set in `replayed["kind"]` (or, in turn, those in `replayed["kinds"]`)."""
    seen: dict[str, Any] = {"kind": "success", "kinds": []}

    @contextmanager
    def fake_session(*args: Any, **kwargs: Any) -> Iterator[None]:
        yield None

    def fake_replay(capability: Capability, params: dict[str, Any], session: Any) -> Any:
        seen["capability"] = capability
        kind = seen["kinds"].pop(0) if seen["kinds"] else seen["kind"]
        return TypeAdapter(RunResult).validate_python({
            "kind": kind, "run_id": "r", "capability_id": capability.id,
            "capability_version": capability.version, "started_at": NOW, "finished_at": NOW,
            "evidence_ref": "evidence/r", **RESULTS[kind]})

    monkeypatch.setattr(workflows, "open_session", fake_session)
    monkeypatch.setattr(workflows, "run_replay", fake_replay)
    return seen


@pytest.fixture
def cleared(monkeypatch: pytest.MonkeyPatch) -> None:
    """For tests about something else: every capability counts as cleared for unattended
    replay (the gate has its own tests below)."""
    monkeypatch.setattr(workflows, "clearance", lambda *args: [])


def approved_then_newer_draft(store: Store) -> None:
    store.save(draft(), {"kind": "success"})
    store.approve(CAP, "0.1.0", "alice")
    store.save(draft().model_copy(update={"version": "0.1.1"}))


@pytest.mark.usefixtures("cleared")
def test_replay_defaults_to_the_latest_approved_version(store: Store,
                                                         replayed: dict[str, Any]) -> None:
    approved_then_newer_draft(store)
    assert runner.invoke(cli.app, ["replay", CAP]).exit_code == 0
    assert replayed["capability"].version == "0.1.0"
    # A draft only runs when asked for by version.
    assert runner.invoke(cli.app, ["replay", CAP, "--version", "0.1.1"]).exit_code == 0
    assert replayed["capability"].version == "0.1.1"


def test_replay_without_an_approved_version_is_a_usage_error(store: Store,
                                                             replayed: dict[str, Any]) -> None:
    store.save(draft())
    result = runner.invoke(cli.app, ["replay", CAP])
    assert result.exit_code == 2
    assert "no approved version" in result.output and "Traceback" not in result.output
    assert "capability" not in replayed


@pytest.mark.usefixtures("cleared")
@pytest.mark.parametrize("kind,code", [("success", 0), ("failure", 1),
                                       ("business_outcome", 3), ("aborted", 4)])
def test_exit_code_mirrors_the_result_kind(store: Store, replayed: dict[str, Any],
                                           kind: str, code: int) -> None:
    approved_then_newer_draft(store)
    replayed["kind"] = kind
    result = runner.invoke(cli.app, ["replay", CAP])
    assert result.exit_code == code
    assert f'"kind": "{kind}"' in result.output


@pytest.mark.parametrize("params", ["{not json", "[1, 2]"])
def test_bad_params_are_a_usage_error(store: Store, replayed: dict[str, Any],
                                      params: str) -> None:
    approved_then_newer_draft(store)
    result = runner.invoke(cli.app, ["replay", CAP, "--params", params])
    assert result.exit_code == 2 and "--params" in result.output
    assert "capability" not in replayed


def test_approve_errors_are_one_line(store: Store) -> None:
    approved_then_newer_draft(store)
    result = runner.invoke(cli.app, ["approve", CAP, "0.1.0", "--reviewer", "bob"])
    assert result.exit_code == 2
    assert "is approved, not draft" in result.output and "Traceback" not in result.output


def test_discover_needs_a_key_before_opening_anything(
        store: Store, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)  # no .env to load the key from
    monkeypatch.setattr(workflows, "open_session", lambda *a, **k: pytest.fail("opened a session"))
    result = runner.invoke(cli.app, ["discover", "goal.yaml"])
    assert result.exit_code == 2 and "ANTHROPIC_API_KEY" in result.output


@pytest.mark.usefixtures("cleared")
def test_replay_with_a_tenant_applies_only_its_approved_overlay(
        store: Store, replayed: dict[str, Any]) -> None:
    approved_then_newer_draft(store)
    store.save_overlay(overlay(store.load(CAP, "0.1.0")), {"kind": "success"})
    result = runner.invoke(cli.app, ["replay", CAP, "--tenant", "riverbend"])
    assert result.exit_code == 2 and "overlay" in result.output and "is draft" in result.output
    store.approve_overlay("riverbend", CAP, "0.1.0", "bob")
    assert runner.invoke(cli.app, ["replay", CAP, "--tenant", "riverbend"]).exit_code == 0
    assert replayed["capability"].version == "0.1.0+riverbend"
    # a tenant with no overlay runs the base as recorded
    assert runner.invoke(cli.app, ["replay", CAP, "--tenant", "pinnacle"]).exit_code == 0
    assert replayed["capability"].version == "0.1.0"


def test_approving_an_overlay(store: Store) -> None:
    approved_then_newer_draft(store)
    store.save_overlay(overlay(store.load(CAP, "0.1.0")), {"kind": "success"})
    args = ["approve", CAP, "0.1.0", "--reviewer", "bob", "--tenant", "riverbend"]
    result = runner.invoke(cli.app, [*args, "--read-only", "search"])
    assert result.exit_code == 2 and "no risk to lower" in result.output
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    assert store.overlay("riverbend", CAP, "0.1.0").status == "approved"  # type: ignore[union-attr]


# --- unattended clearance ---------------------------------------------------------------------

LOOKUP_PARAMS = ["--params", '{"member_id": "12345"}']


def test_unattended_replay_needs_a_stability_report(store: Store, replayed: dict[str, Any],
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    approved_then_newer_draft(store)
    result = runner.invoke(cli.app, ["replay", CAP, *LOOKUP_PARAMS])
    assert result.exit_code == 2 and "no stability report" in result.output
    assert "capability" not in replayed
    # with a person watching, approved is enough
    monkeypatch.setattr(cli, "operator_control", lambda mode: (InMemoryControl(), False))
    result = runner.invoke(cli.app, ["replay", CAP, *LOOKUP_PARAMS, "--operator", "console"])
    assert result.exit_code == 0 and replayed["capability"].version == "0.1.0"


def test_a_stable_measurement_clears_unattended_replay(store: Store,
                                                       replayed: dict[str, Any]) -> None:
    approved_then_newer_draft(store)
    result = runner.invoke(cli.app, ["stability", CAP, *LOOKUP_PARAMS,
                                     "--params", '{"member_id": "45678"}'])
    assert result.exit_code == 0, result.output
    assert "stable: 100% success over 10 runs" in result.output
    report = store.stability(CAP, "0.1.0")
    assert report is not None and [r.params_set for r in report.runs[:3]] == [0, 1, 0]
    assert runner.invoke(cli.app, ["replay", CAP, *LOOKUP_PARAMS]).exit_code == 0


def test_a_flaky_measurement_is_saved_but_does_not_clear(store: Store,
                                                          replayed: dict[str, Any]) -> None:
    approved_then_newer_draft(store)
    replayed["kinds"] = ["success", "failure"] * 5
    result = runner.invoke(cli.app, ["stability", CAP, *LOOKUP_PARAMS])
    assert result.exit_code == 1 and "flaky: 50% success" in result.output
    assert "succeeded 5/10" in result.output
    assert store.stability(CAP, "0.1.0").verdict == "flaky"  # type: ignore[union-attr]
    assert runner.invoke(cli.app, ["replay", CAP, *LOOKUP_PARAMS]).exit_code == 2


def test_irreversible_capabilities_are_never_measured(store: Store,
                                                      replayed: dict[str, Any]) -> None:
    cap = load_capability("corebank.subaccount.open")
    store.save(cap)
    result = runner.invoke(cli.app, ["stability", cap.id, "--params", "{}"])
    assert result.exit_code == 2 and "irreversible" in result.output
    assert "capability" not in replayed


def test_reachable() -> None:
    assert not cli.reachable("http://127.0.0.1:1", timeout_s=0.5)
