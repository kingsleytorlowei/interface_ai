"""Tenant overlays: a base capability recorded on one tenant, run on another by replacing the
targets that differ there, and nothing else."""

import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from conftest import CATALOG, OpenSession, load_capability
from cua.replay import replay
from cua.schema import Capability, CapabilityOverlay, Status, Success, apply_overlay
from cua.store import Store, StoreError

LOOKUP = load_capability("corebank.member.lookup_balance")
FRAME = [{"name": "content"}]
RIVERBEND_TARGETS: dict[str, Any] = {
    "member_id_field": {"frame": FRAME, "strategies": [
        {"by": "near", "text": "Member #", "direction": "right", "role": "textbox"}]},
    "search_button": {"frame": FRAME, "strategies": [
        {"by": "role", "role": "button", "name": "Find"}]},
    "member_name": {"frame": FRAME, "strategies": [
        {"by": "near", "text": "Member Name:", "direction": "right", "role": "cell"}]},
    "share_savings_balance": {"frame": FRAME, "strategies": [
        {"by": "table_cell", "row_has_text": "Share Savings", "column": "Current Bal."}]},
}


def overlay(base: Capability = LOOKUP, *, status: Status = Status.DRAFT,
            **fields: Any) -> CapabilityOverlay:
    return CapabilityOverlay.model_validate({
        "tenant": "riverbend", "base": {"id": base.id, "version": base.version},
        "status": status, "targets": RIVERBEND_TARGETS,
        "provenance": {"authored_by": "alice", "source": "a drift run",
                       "created_at": datetime.now(UTC),
                       "reviewed_by": "bob" if status is Status.APPROVED else None},
        **fields,
    })


# --- merge rules ----------------------------------------------------------------------------


def test_overlay_replaces_targets_and_names_the_tenant_in_the_version() -> None:
    merged = apply_overlay(LOOKUP, overlay(status=Status.APPROVED))
    assert merged.version == f"{LOOKUP.version}+riverbend"
    assert merged.status is Status.APPROVED
    assert merged.targets["search_button"].strategies[0].name == "Find"  # type: ignore[union-attr]
    assert merged.steps == LOOKUP.steps and merged.outputs == LOOKUP.outputs


def test_a_draft_overlay_makes_the_capability_a_draft() -> None:
    assert apply_overlay(LOOKUP, overlay()).status is Status.DRAFT


def test_overlay_cannot_add_targets_or_apply_to_another_version() -> None:
    with pytest.raises(ValueError, match="doesn't have"):
        apply_overlay(LOOKUP, overlay(targets={**RIVERBEND_TARGETS, "extra": RIVERBEND_TARGETS[
            "search_button"]}))
    with pytest.raises(ValueError, match="overlay is for"):
        apply_overlay(LOOKUP.model_copy(update={"version": "9.9.9"}), overlay())


def test_overlay_can_only_carry_targets() -> None:
    with pytest.raises(ValidationError, match="steps"):
        overlay(steps=[])
    with pytest.raises(ValidationError, match="reviewed_by"):
        CapabilityOverlay.model_validate({**overlay().model_dump(), "status": "approved"})


# --- store ----------------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> Store:
    shutil.copytree(CATALOG / "corebank", tmp_path / "corebank",
                    ignore=shutil.ignore_patterns("capabilities", "tenants"))
    store = Store(tmp_path)
    store.save(LOOKUP)
    return store


def test_a_tenant_without_an_overlay_runs_the_base(store: Store) -> None:
    assert store.load(LOOKUP.id, status=Status.APPROVED, tenant="riverbend") == LOOKUP


def test_a_draft_overlay_is_never_skipped(store: Store) -> None:
    store.save_overlay(overlay())
    with pytest.raises(StoreError, match="is draft"):
        store.load(LOOKUP.id, status=Status.APPROVED, tenant="riverbend")
    # asked for explicitly (like `--version`), the draft overlay applies
    assert store.load(LOOKUP.id, LOOKUP.version, tenant="riverbend").version.endswith(
        "+riverbend")


def test_overlay_approval_needs_verification_and_is_then_immutable(store: Store) -> None:
    store.save_overlay(overlay())
    with pytest.raises(StoreError, match="verification"):
        store.approve_overlay("riverbend", LOOKUP.id, LOOKUP.version, "bob")
    store.save_overlay(overlay(), {"kind": "success"})
    approved = store.approve_overlay("riverbend", LOOKUP.id, LOOKUP.version, "bob")
    assert approved.provenance.reviewed_by == "bob"
    assert store.load(LOOKUP.id, status=Status.APPROVED, tenant="riverbend").status is (
        Status.APPROVED)
    with pytest.raises(StoreError, match="immutable"):
        store.save_overlay(overlay())


def test_overlays_only_sit_on_an_approved_base(store: Store) -> None:
    draft = LOOKUP.model_copy(update={"version": "0.2.0", "status": Status.DRAFT})
    store.save(draft)
    with pytest.raises(StoreError, match="approved base"):
        store.save_overlay(overlay(draft))


def test_a_merged_capability_is_never_stored(store: Store) -> None:
    with pytest.raises(StoreError, match="separately"):
        store.save(apply_overlay(LOOKUP, overlay()))


# --- live -----------------------------------------------------------------------------------


def test_base_plus_overlay_succeeds_on_the_other_tenant(open_session: OpenSession) -> None:
    """The base alone fails on riverbend (test_replay); with the overlay it succeeds, with no
    drift, and the result names the overlay it ran with."""
    merged = apply_overlay(LOOKUP, overlay(status=Status.APPROVED))
    result = replay(merged, {"member_id": "12345"}, open_session(variant="riverbend"))
    assert isinstance(result, Success), result
    assert result.capability_version == f"{LOOKUP.version}+riverbend"
    assert result.drift == []
    assert result.outputs["member_name"] == "Jane Q. Sample"
