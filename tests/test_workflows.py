"""Rules that belong to the actions themselves, whichever front end asks (the CLI tests cover
the command line's own contract)."""

import shutil
from pathlib import Path

import pytest

from conftest import CATALOG, load_capability
from cua import workflows
from cua.control import InMemoryControl
from cua.schema import Status
from cua.store import Store
from cua.workflows import WorkflowError, Workspace

LOOKUP = load_capability("corebank.member.lookup_balance")


@pytest.fixture
def ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    shutil.copytree(CATALOG / "corebank", tmp_path / "corebank",
                    ignore=shutil.ignore_patterns("capabilities", "tenants"))
    # nothing here may reach a browser
    monkeypatch.setattr(workflows, "open_session",
                        lambda *a, **k: pytest.fail("opened a session"))
    return Workspace(Store(tmp_path), tmp_path / "evidence", "http://127.0.0.1:1")


def test_unattended_replay_is_refused_before_a_browser_opens(ws: Workspace) -> None:
    with pytest.raises(WorkflowError, match="no stability report"):
        workflows.replay_capability(ws, LOOKUP, {"member_id": "12345"},
                                    control=InMemoryControl(), attended=False)


def test_approval_needs_a_name(ws: Workspace) -> None:
    with pytest.raises(WorkflowError, match="reviewer's name"):
        workflows.approve(ws, LOOKUP.id, LOOKUP.version, "  ")


def test_only_approved_capabilities_are_measured(ws: Workspace) -> None:
    draft = LOOKUP.model_copy(update={"status": Status.DRAFT})
    with pytest.raises(WorkflowError, match="only approved"):
        workflows.measure_stability(ws, draft, [{"member_id": "12345"}])
    with pytest.raises(WorkflowError, match="at least one set"):
        workflows.measure_stability(ws, LOOKUP, [])
