import shutil
from pathlib import Path

import pytest

from conftest import CATALOG, load_capability
from cua.schema import Capability, Risk, Status
from cua.store import Store, StoreError


@pytest.fixture
def store(tmp_path: Path) -> Store:
    shutil.copytree(CATALOG / "corebank", tmp_path / "corebank",
                    ignore=shutil.ignore_patterns("capabilities"))
    return Store(tmp_path)


def draft() -> Capability:
    """The lookup fixture as discovery would leave it: a draft with Search recorded as the
    policy enforced it (reversible)."""
    cap = load_capability("corebank.member.lookup_balance")
    data = cap.model_dump()
    data["status"] = Status.DRAFT
    data["version"] = "0.1.0"
    data["provenance"]["reviewed_by"] = None
    for step in data["steps"]:
        if step["id"] == "search":
            step["risk"] = Risk.REVERSIBLE
    data["risk"] = Risk.REVERSIBLE
    return Capability.model_validate(data)


def test_approval_needs_a_successful_verification(store: Store) -> None:
    cap = draft()
    store.save(cap)
    with pytest.raises(StoreError, match="verification"):
        store.approve(cap.id, "0.1.0", "alice")
    store.save(cap, {"kind": "failure"})
    with pytest.raises(StoreError, match="failure"):
        store.approve(cap.id, "0.1.0", "alice")

    store.save(cap, {"kind": "success"})
    approved = store.approve(cap.id, "0.1.0", "alice", read_only_steps=["search"])
    assert approved.status is Status.APPROVED and approved.provenance.reviewed_by == "alice"
    assert approved.risk is Risk.READ_ONLY
    assert store.load(cap.id) == approved


def test_approval_can_only_lower_reversible_steps(store: Store) -> None:
    cap = draft()
    store.save(cap, {"kind": "success"})
    with pytest.raises(StoreError, match="only reversible"):
        store.approve(cap.id, "0.1.0", "alice", read_only_steps=["read_balance"])
    with pytest.raises(StoreError, match="unknown steps"):
        store.approve(cap.id, "0.1.0", "alice", read_only_steps=["nope"])


def test_new_drafts_never_collide_with_approved_versions(store: Store) -> None:
    cap = draft()
    assert store.next_draft_version(cap.id) == "0.1.0"
    store.save(cap, {"kind": "failure"})
    assert store.next_draft_version(cap.id) == "0.1.0"  # an unapproved draft is replaced
    store.save(cap, {"kind": "success"})
    store.approve(cap.id, "0.1.0", "alice")
    assert store.next_draft_version(cap.id) == "0.1.1"
    store.save(cap.model_copy(update={"version": "0.1.1"}))
    assert store.next_draft_version(cap.id) == "0.1.1"


def test_approved_versions_are_immutable_and_latest_wins(store: Store) -> None:
    cap = draft()
    store.save(cap, {"kind": "success"})
    store.approve(cap.id, "0.1.0", "alice")
    with pytest.raises(StoreError, match="immutable"):
        store.save(cap)
    newer = cap.model_copy(update={"version": "0.10.0"})
    store.save(newer)
    assert store.versions(cap.id) == ["0.1.0", "0.10.0"]  # semver order, not string order
    assert store.load(cap.id).version == "0.10.0"
    with pytest.raises(StoreError):
        store.load("corebank.nothing.here")


def test_loading_by_status_skips_newer_versions_in_another_status(store: Store) -> None:
    cap = draft()
    store.save(cap)
    with pytest.raises(StoreError, match="no approved version"):
        store.load(cap.id, status=Status.APPROVED)
    store.save(cap, {"kind": "success"})
    store.approve(cap.id, "0.1.0", "alice")
    store.save(cap.model_copy(update={"version": "0.1.1"}))
    assert store.load(cap.id).version == "0.1.1"
    assert store.load(cap.id, status=Status.APPROVED).version == "0.1.0"
