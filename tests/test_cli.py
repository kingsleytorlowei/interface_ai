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

from conftest import CATALOG
from cua import cli
from cua.schema import Capability, RunResult
from cua.store import Store
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
                    ignore=shutil.ignore_patterns("capabilities"))
    monkeypatch.setattr(cli, "CATALOG", tmp_path)
    return Store(tmp_path)


@pytest.fixture
def replayed(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Fakes the session and the replay; records which capability was replayed and returns
    the result kind set in `replayed["kind"]`."""
    seen: dict[str, Any] = {"kind": "success"}

    @contextmanager
    def fake_session(*args: Any, **kwargs: Any) -> Iterator[None]:
        yield None

    def fake_replay(capability: Capability, params: dict[str, Any], session: Any) -> Any:
        seen["capability"] = capability
        return TypeAdapter(RunResult).validate_python({
            "kind": seen["kind"], "run_id": "r", "capability_id": capability.id,
            "capability_version": capability.version, "started_at": NOW, "finished_at": NOW,
            "evidence_ref": "evidence/r", **RESULTS[seen["kind"]]})

    monkeypatch.setattr(cli, "open_session", fake_session)
    monkeypatch.setattr(cli, "run_replay", fake_replay)
    return seen


def approved_then_newer_draft(store: Store) -> None:
    store.save(draft(), {"kind": "success"})
    store.approve(CAP, "0.1.0", "alice")
    store.save(draft().model_copy(update={"version": "0.1.1"}))


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
    monkeypatch.setattr(cli, "open_session", lambda *a, **k: pytest.fail("opened a session"))
    result = runner.invoke(cli.app, ["discover", "goal.yaml"])
    assert result.exit_code == 2 and "ANTHROPIC_API_KEY" in result.output
