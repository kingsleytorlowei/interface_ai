"""How reliably a capability replays, measured by replaying it: the evidence behind clearing
an approved capability for unattended use.

Approval is a person's judgement of what a capability does; stability is a measurement of
how reliably it does it, on one tenant, at one version. The summary is derived from the runs
and checked against them on load, so a report can't claim more than its runs show.
"""

from collections import Counter
from datetime import datetime
from enum import StrEnum
from statistics import median
from typing import Any, Literal

from pydantic import Field, model_validator

from .common import BuildVersion, DottedId, Model, Slug


class Verdict(StrEnum):
    STABLE = "stable"  # every run succeeded, and every target resolved by its first strategy
    DRIFTING = "drifting"  # every run succeeded, some through fallback strategies
    FLAKY = "flaky"  # some runs succeeded, or identical inputs gave different results
    BROKEN = "broken"  # no run succeeded


class StabilityRun(Model):
    run_id: str
    evidence_ref: str
    params_set: int  # which --params set (values are sensitive and never stored)
    kind: str
    category: str | None = None
    step_id: str | None = None
    drift_targets: list[str] = []
    recoveries: list[str] = []  # the app misbehaving and the engine coping: reported, not scored
    duration_s: float


def summarize(runs: list[StabilityRun]) -> dict[str, Any]:
    successes = sum(r.kind == "success" for r in runs)
    kinds_per_set: dict[int, set[str]] = {}
    for r in runs:
        kinds_per_set.setdefault(r.params_set, set()).add(r.kind)
    consistent = all(len(kinds) == 1 for kinds in kinds_per_set.values())
    drift_runs = sum(bool(r.drift_targets) for r in runs)
    if successes == 0:
        verdict = Verdict.BROKEN
    elif successes < len(runs) or not consistent:
        verdict = Verdict.FLAKY
    elif drift_runs:
        verdict = Verdict.DRIFTING
    else:
        verdict = Verdict.STABLE
    durations = [r.duration_s for r in runs]
    return {
        "runs_total": len(runs),
        "success_rate": round(successes / len(runs), 4),
        "consistent": consistent,
        "drift_runs": drift_runs,
        "recovery_runs": sum(bool(r.recoveries) for r in runs),
        "failures": dict(Counter(f"{r.kind}/{r.category or '-'}@{r.step_id or '-'}"
                                 for r in runs if r.kind != "success")),
        "duration_p50_s": round(median(durations), 2),
        "duration_max_s": round(max(durations), 2),
        "verdict": verdict,
    }


class StabilityReport(Model):
    schema_version: Literal["1"] = "1"
    capability_id: DottedId
    version: BuildVersion  # as run, e.g. 0.1.0+riverbend: a new base or overlay starts over
    tenant: Slug | None = None
    measured_at: datetime
    runs: list[StabilityRun] = Field(min_length=1)
    # derived from `runs` (see `summarize`); kept in the file so a reviewer can read it
    runs_total: int
    success_rate: float
    consistent: bool
    drift_runs: int
    recovery_runs: int
    failures: dict[str, int]
    duration_p50_s: float
    duration_max_s: float
    verdict: Verdict

    @classmethod
    def from_runs(cls, capability_id: str, version: str, tenant: str | None,
                  measured_at: datetime, runs: list[StabilityRun]) -> "StabilityReport":
        return cls(capability_id=capability_id, version=version, tenant=tenant,
                   measured_at=measured_at, runs=runs, **summarize(runs))

    @model_validator(mode="after")
    def _summary_matches_runs(self) -> "StabilityReport":
        stated = self.model_dump(include=set(summarize(self.runs)))
        if stated != summarize(self.runs):
            raise ValueError("the summary doesn't match the runs")
        _, _, tenant = self.version.partition("+")
        if (tenant or None) != self.tenant:
            raise ValueError(f"version {self.version} and tenant {self.tenant} disagree")
        return self
