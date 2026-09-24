"""Clearance for unattended replay: approved is necessary, not sufficient.

A person approves what a capability does; whether it may run with nobody watching is decided
by measurement, against the app's thresholds (`policy.json` → `unattended`), for the exact
version that will run (a tenant's overlay is its own version). This is a run-level decision
made before a session opens, so it lives here and not in the per-action policy, which has
no notion of whether anyone is watching.
"""

from datetime import datetime, timedelta

from cua.policy import UnattendedThresholds
from cua.schema import Capability, Risk, StabilityReport, Status


def clearance(capability: Capability, report: StabilityReport | None,
              thresholds: UnattendedThresholds, now: datetime) -> list[str]:
    """Why `capability` may not run unattended; empty means it may."""
    if capability.status is not Status.APPROVED:
        return [f"{capability.id}@{capability.version} is {capability.status}, not approved"]
    if capability.risk is Risk.IRREVERSIBLE:
        return ["it has irreversible steps, which need a person's approval on every run"]
    if report is None:
        return [f"no stability report for {capability.id}@{capability.version}; "
                "run `cua stability`"]
    if (report.capability_id, report.version) != (capability.id, capability.version):
        return [f"the stability report is for {report.capability_id}@{report.version}"]
    reasons: list[str] = []
    if report.runs_total < thresholds.min_runs:
        reasons.append(f"measured over {report.runs_total} runs, needs {thresholds.min_runs}")
    if report.success_rate < thresholds.min_success_rate:
        successes = round(report.success_rate * report.runs_total)
        reasons.append(f"succeeded {successes}/{report.runs_total} "
                       f"(needs {thresholds.min_success_rate:.0%}): {report.failures}")
    if not report.consistent:
        reasons.append("identical inputs gave different results")
    if report.drift_runs > thresholds.max_drift_runs:
        reasons.append(f"{report.drift_runs} runs resolved targets through fallbacks "
                       f"(allows {thresholds.max_drift_runs})")
    if now - report.measured_at > timedelta(days=thresholds.max_age_days):
        reasons.append(f"measured {(now - report.measured_at).days} days ago "
                       f"(valid for {thresholds.max_age_days})")
    return reasons
