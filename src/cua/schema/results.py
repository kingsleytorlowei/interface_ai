"""The replay result contract returned to the calling agent.

Exactly one terminal kind per run: `success`, `business_outcome` (a legitimate result such as
"no such member"), `failure` (hard stop with enough detail to debug) or `aborted` (a human
stopped it). Recoverable conditions are not a terminal kind: they are handled in-run and listed
in `recoveries`. Escalation is not terminal either: the run pauses, a human acts, and the run
still ends in one of the kinds above with the handoff recorded in `interventions`.
"""

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field

from .common import Model

OutputValue = str | int | Decimal | bool | date


class RecoveryRecord(Model):
    step_id: str | None
    state: str
    recovery: str
    attempt: int


class DriftSignal(Model):
    """A target resolved, but not via its preferred strategy — the UI may be drifting."""

    step_id: str
    target: str
    strategy_index: int


class HumanAction(Model):
    kind: str
    target_description: str
    value: str | None = None  # redacted per policy before it gets here
    at: datetime


class Intervention(Model):
    id: str
    step_id: str | None
    reason: str
    operator: str | None
    actions: list[HumanAction] = []
    resolution: Literal["resumed", "aborted"]
    requested_at: datetime
    resolved_at: datetime


class _ResultBase(Model):
    run_id: str
    capability_id: str
    capability_version: str
    started_at: datetime
    finished_at: datetime
    recoveries: list[RecoveryRecord] = []
    interventions: list[Intervention] = []
    drift: list[DriftSignal] = []
    evidence_ref: str


class Success(_ResultBase):
    kind: Literal["success"] = "success"
    outputs: dict[str, OutputValue]


class BusinessOutcome(_ResultBase):
    kind: Literal["business_outcome"] = "business_outcome"
    code: str
    message: str


class FailureCategory(StrEnum):
    INVALID_INPUT = "invalid_input"
    POLICY_DENIED = "policy_denied"
    TARGET_NOT_FOUND = "target_not_found"
    TARGET_AMBIGUOUS = "target_ambiguous"
    CHECKPOINT_MISMATCH = "checkpoint_mismatch"
    UNKNOWN_STATE = "unknown_state"
    RECOVERY_EXHAUSTED = "recovery_exhausted"
    TIMEOUT = "timeout"
    APP_ERROR = "app_error"


class Failure(_ResultBase):
    kind: Literal["failure"] = "failure"
    category: FailureCategory
    step_id: str | None
    expected: str
    observed: str
    retryable: bool
    message: str


class Aborted(_ResultBase):
    kind: Literal["aborted"] = "aborted"
    step_id: str | None
    reason: str


RunResult = Annotated[Success | BusinessOutcome | Failure | Aborted, Field(discriminator="kind")]
