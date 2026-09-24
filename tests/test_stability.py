"""The stability report (derived from its runs, and checked against them) and the clearance
rule for unattended replay."""

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from conftest import CATALOG, load_capability
from cua.policy import UnattendedThresholds
from cua.schema import StabilityReport, StabilityRun, Status, Verdict
from cua.stability import clearance
from cua.store import Store

LOOKUP = load_capability("corebank.member.lookup_balance")
NOW = datetime.now(UTC)
THRESHOLDS = UnattendedThresholds()  # 10 runs, 100%, no drift, 30 days


def run(kind: str = "success", *, params_set: int = 0, drift: bool = False,
        recoveries: tuple[str, ...] = ()) -> StabilityRun:
    return StabilityRun(
        run_id="r", evidence_ref="evidence/r", params_set=params_set, kind=kind,
        category="timeout" if kind == "failure" else None,
        step_id="search" if kind == "failure" else None,
        drift_targets=["search_button"] if drift else [], recoveries=list(recoveries),
        duration_s=3.0)


def report(runs: list[StabilityRun], *, version: str = LOOKUP.version, tenant: str | None = None,
           measured_at: datetime = NOW) -> StabilityReport:
    return StabilityReport.from_runs(LOOKUP.id, version, tenant, measured_at, runs)


# --- the report -----------------------------------------------------------------------------


@pytest.mark.parametrize("runs,verdict", [
    ([run()] * 10, Verdict.STABLE),
    ([run(recoveries=("retry",))] + [run()] * 9, Verdict.STABLE),  # recoveries aren't scored
    ([run(drift=True)] + [run()] * 9, Verdict.DRIFTING),
    ([run("failure")] + [run()] * 9, Verdict.FLAKY),
    ([run("failure")] * 10, Verdict.BROKEN),
])
def test_verdict(runs: list[StabilityRun], verdict: Verdict) -> None:
    assert report(runs).verdict is verdict


def test_identical_inputs_with_different_results_are_inconsistent() -> None:
    mixed = report([run("business_outcome", params_set=0), run(params_set=0),
                    run(params_set=1), run(params_set=1)])
    assert not mixed.consistent and mixed.verdict is Verdict.FLAKY
    # different inputs may legitimately end differently
    assert report([run("business_outcome", params_set=0), run(params_set=1)]).consistent


def test_a_report_cannot_claim_more_than_its_runs_show() -> None:
    data: dict[str, Any] = report([run("failure")] + [run()] * 9).model_dump()
    data.update(success_rate=1.0, verdict="stable")
    with pytest.raises(ValidationError, match="doesn't match the runs"):
        StabilityReport.model_validate(data)


def test_the_version_names_the_tenant() -> None:
    assert report([run()], version="0.1.0+riverbend", tenant="riverbend").tenant == "riverbend"
    with pytest.raises(ValidationError, match="disagree"):
        report([run()], version="0.1.0+riverbend")


# --- clearance ------------------------------------------------------------------------------


def test_ten_clean_runs_clear_an_approved_capability() -> None:
    assert clearance(LOOKUP, report([run()] * 10), THRESHOLDS, NOW) == []


@pytest.mark.parametrize("runs,reason", [
    ([run()] * 9, "measured over 9 runs, needs 10"),
    ([run("failure")] + [run()] * 9, "succeeded 9/10"),
    ([run(drift=True)] + [run()] * 9, "1 runs resolved targets through fallbacks"),
])
def test_what_blocks_clearance(runs: list[StabilityRun], reason: str) -> None:
    assert any(reason in r for r in clearance(LOOKUP, report(runs), THRESHOLDS, NOW))


def test_clearance_goes_stale() -> None:
    old = report([run()] * 10, measured_at=NOW - timedelta(days=31))
    assert clearance(LOOKUP, old, THRESHOLDS, NOW) == ["measured 31 days ago (valid for 30)"]


def test_no_report_or_a_report_for_another_version_does_not_clear() -> None:
    assert "no stability report" in clearance(LOOKUP, None, THRESHOLDS, NOW)[0]
    overlaid = report([run()] * 10, version="1.0.0+riverbend", tenant="riverbend")
    assert "is for" in clearance(LOOKUP, overlaid, THRESHOLDS, NOW)[0]


def test_drafts_and_irreversible_capabilities_are_never_cleared() -> None:
    draft = LOOKUP.model_copy(update={"status": Status.DRAFT})
    assert "not approved" in clearance(draft, report([run()] * 10), THRESHOLDS, NOW)[0]
    open_subaccount = load_capability("corebank.subaccount.open")
    assert "irreversible" in clearance(open_subaccount, None, THRESHOLDS, NOW)[0]


# --- store ----------------------------------------------------------------------------------


def test_reports_sit_beside_what_they_measured(tmp_path: Path) -> None:
    shutil.copytree(CATALOG / "corebank", tmp_path / "corebank",
                    ignore=shutil.ignore_patterns("capabilities", "tenants"))
    store = Store(tmp_path)
    store.save(LOOKUP)
    base = store.save_stability(report([run()] * 10))
    tenant = store.save_stability(report([run()] * 10, version=f"{LOOKUP.version}+riverbend",
                                         tenant="riverbend"))
    assert base.parent.name == "capabilities" and tenant.parent.name == "riverbend"
    assert store.stability(LOOKUP.id, LOOKUP.version) == report([run()] * 10)
    assert store.versions(LOOKUP.id) == [LOOKUP.version]  # not mistaken for a version
