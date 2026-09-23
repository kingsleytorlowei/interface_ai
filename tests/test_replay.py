"""Replay against the live mock bank: every terminal kind, every recovery, and the handoff."""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from conftest import OpenSession, load_capability
from cua.control import InMemoryControl, Resolution
from cua.replay import replay
from cua.schema import (
    Aborted,
    BusinessOutcome,
    Capability,
    Checkpoint,
    Failure,
    FailureCategory,
    HumanAction,
    Success,
    Target,
)
from cua.schema.targets import ByRole, FrameSelector
from cua.surface.web import WebSurface

LOOKUP = load_capability("corebank.member.lookup_balance")
OPEN_SUBACCOUNT = load_capability("corebank.subaccount.open")
NO_WAIT = {"sleep": lambda _: None}


def fault(base: str, **faults: int) -> None:
    httpx.put(f"{base}/__admin/faults", json=faults).raise_for_status()


def with_step(cap: Capability, step_id: str, **update: object) -> Capability:
    steps = [s.model_copy(update=update) if s.id == step_id else s for s in cap.steps]
    return cap.model_copy(update={"steps": steps})


def event_kinds(result) -> list[str]:  # type: ignore[no-untyped-def]
    lines = open(f"{result.evidence_ref}/events.jsonl").read().splitlines()
    return [json.loads(line)["kind"] for line in lines]


# --- lookup ---------------------------------------------------------------------------------


def test_lookup_success_is_typed_and_redacted_in_evidence(open_session: OpenSession) -> None:
    result = replay(LOOKUP, {"member_id": "12345"}, open_session(sign_on=False))
    assert isinstance(result, Success), result
    assert result.outputs == {"member_name": "Jane Q. Sample",
                              "share_savings_balance": Decimal("4210.37")}
    assert result.recoveries == result.interventions == result.drift == []
    assert result.committed_steps == []

    evidence = "".join(open(f"{result.evidence_ref}/{f}").read()
                       for f in ("events.jsonl", "result.json"))
    for sensitive in ("12345", "Jane Q. Sample", "4,210.37", "4210.37"):
        assert sensitive not in evidence
    assert "«inputs.member_id»" in evidence and "«share_savings_balance»" in evidence


@pytest.mark.parametrize(("member_id", "code"), [("99999", "member_not_found"),
                                                 ("34567", "access_denied")])
def test_business_outcomes(open_session: OpenSession, member_id: str, code: str) -> None:
    result = replay(LOOKUP, {"member_id": member_id}, open_session())
    assert isinstance(result, BusinessOutcome) and result.code == code
    assert result.committed_steps == []


def test_invalid_input_never_touches_the_app(open_session: OpenSession) -> None:
    result = replay(LOOKUP, {"member_id": "12a"}, open_session(sign_on=False))
    assert isinstance(result, Failure) and result.category is FailureCategory.INVALID_INPUT
    assert not result.retryable
    assert "action_started" not in event_kinds(result)
    assert "12a" not in open(f"{result.evidence_ref}/result.json").read()


# --- recoveries -----------------------------------------------------------------------------


def test_dismissable_notice_is_clicked_through(open_session: OpenSession) -> None:
    session = open_session()
    fault(session.env["base_url"], broadcast_notices=1)
    result = replay(LOOKUP, {"member_id": "12345"}, session)
    assert isinstance(result, Success)
    assert [(r.state, r.recovery, r.step_id) for r in result.recoveries] == [
        ("system_notice", "click", "search")]


def test_transient_unavailability_restarts_from_entry(open_session: OpenSession) -> None:
    session = open_session()
    fault(session.env["base_url"], transient_failures=1)
    result = replay(LOOKUP, {"member_id": "12345"}, session, **NO_WAIT)
    assert isinstance(result, Success)
    assert [(r.state, r.recovery) for r in result.recoveries] == [("service_unavailable", "retry")]
    assert "run_restarted" in event_kinds(result)


def test_persistent_unavailability_exhausts_recovery(open_session: OpenSession) -> None:
    session = open_session()
    fault(session.env["base_url"], transient_failures=10)
    result = replay(LOOKUP, {"member_id": "12345"}, session, **NO_WAIT)
    assert isinstance(result, Failure)
    assert result.category is FailureCategory.RECOVERY_EXHAUSTED and result.retryable
    assert len(result.recoveries) == 3


def test_expired_session_reauthenticates(open_session: OpenSession) -> None:
    session = open_session()
    httpx.post(f"{session.env['base_url']}/__admin/expire-sessions").raise_for_status()
    result = replay(LOOKUP, {"member_id": "12345"}, session)
    assert isinstance(result, Success)
    assert [(r.state, r.recovery) for r in result.recoveries] == [("signed_out", "reauthenticate")]


def test_fatal_screen_fails_with_evidence(open_session: OpenSession) -> None:
    session = open_session()
    fault(session.env["base_url"], fatal_errors=1)
    result = replay(LOOKUP, {"member_id": "12345"}, session)
    assert isinstance(result, Failure)
    assert (result.category, result.step_id, result.retryable) == (
        FailureCategory.APP_ERROR, "search", True)
    assert list((session.log.dir / "snapshots").glob("*-fatal.png"))


# --- handoff --------------------------------------------------------------------------------

ACKNOWLEDGE = Target(frame=[FrameSelector(name="content")],
                     strategies=[ByRole(role="button", name="Acknowledge")])


def operator_acknowledges(control_ref: list[InMemoryControl], browser: WebSurface
                          ) -> Callable[..., Resolution]:
    """A human takes the lease, acts on the live window, and hands back."""

    def act(_: object) -> Resolution:
        control_ref[0].acquire("operator:alice")
        browser.click(browser.resolve(ACKNOWLEDGE))
        browser.settle()
        return Resolution("resumed", "alice", (HumanAction(
            kind="click", target_description='button "Acknowledge"', at=datetime.now(UTC)),))

    return act


def test_member_alert_is_handed_to_a_human_and_resumed(
    open_session: OpenSession, browser: WebSurface
) -> None:
    ref: list[InMemoryControl] = []
    control = InMemoryControl(intervene=operator_acknowledges(ref, browser))
    ref.append(control)
    result = replay(LOOKUP, {"member_id": "23456"}, open_session(control=control))
    assert isinstance(result, Success), result
    assert result.outputs["member_name"] == "Robert T. Example"
    [iv] = result.interventions
    assert (iv.operator, iv.resolution, iv.step_id) == ("alice", "resumed", "search")
    assert iv.actions[0].target_description == 'button "Acknowledge"'
    assert [(r.state, r.recovery) for r in result.recoveries] == [("member_alert", "escalate")]


def test_member_alert_without_an_operator_aborts(open_session: OpenSession) -> None:
    result = replay(LOOKUP, {"member_id": "23456"}, open_session())
    assert isinstance(result, Aborted) and "operator aborted" in result.reason
    assert result.interventions[0].resolution == "aborted"


# --- drift and mismatch ---------------------------------------------------------------------


def test_other_tenant_drifts_then_fails_explicitly(open_session: OpenSession) -> None:
    """A pinnacle-recorded lookup on riverbend: fallbacks carry the search (drift is
    reported), then a tenant-specific label has no fallback and the run fails, not guesses."""
    result = replay(LOOKUP, {"member_id": "12345"}, open_session(variant="riverbend"))
    assert isinstance(result, Failure)
    assert (result.category, result.step_id) == (FailureCategory.TARGET_NOT_FOUND,
                                                 "read_member_name")
    assert {(d.target, d.strategy_index) for d in result.drift} == {
        ("member_id_field", 1), ("search_button", 1)}


def test_checkpoint_mismatch_before_commit_fails(open_session: OpenSession) -> None:
    cap = with_step(LOOKUP, "search", expect=Checkpoint(state="subacct_form", timeout_ms=800))
    result = replay(cap, {"member_id": "12345"}, open_session())
    assert isinstance(result, Failure)
    assert (result.category, result.observed, result.retryable) == (
        FailureCategory.CHECKPOINT_MISMATCH, "member_detail", False)


# --- the irreversible flow ------------------------------------------------------------------

PARAMS = {"member_id": "12345", "account_type": "Holiday Club", "initial_deposit": "25.00"}


def test_approved_subaccount_opens_once(open_session: OpenSession) -> None:
    control = InMemoryControl(approve=lambda _: True)
    result = replay(OPEN_SUBACCOUNT, PARAMS, open_session(control=control))
    assert isinstance(result, Success), result
    assert result.outputs == {"confirmation_number": "SA-100231"}
    assert result.committed_steps == ["confirm"]
    [approval] = control.approvals
    assert approval.step_id == "confirm"


def test_rejected_approval_aborts_with_nothing_committed(open_session: OpenSession) -> None:
    result = replay(OPEN_SUBACCOUNT, PARAMS, open_session())  # unattended: rejects
    assert isinstance(result, Aborted) and result.step_id == "confirm"
    assert result.committed_steps == []


def test_app_side_rejection_is_a_business_outcome(open_session: OpenSession) -> None:
    result = replay(OPEN_SUBACCOUNT, {**PARAMS, "initial_deposit": "1.00"}, open_session())
    assert isinstance(result, BusinessOutcome) and result.code == "deposit_rejected"


def test_failure_after_commit_is_not_retryable(open_session: OpenSession) -> None:
    broken = Target(frame=[FrameSelector(name="content")],
                    strategies=[ByRole(role="cell", name="Confirmation Number")])
    cap = OPEN_SUBACCOUNT.model_copy(
        update={"targets": {**OPEN_SUBACCOUNT.targets, "confirmation_number": broken}})
    control = InMemoryControl(approve=lambda _: True)
    result = replay(cap, PARAMS, open_session(control=control))
    assert isinstance(result, Failure)
    assert (result.category, result.step_id) == (FailureCategory.TARGET_NOT_FOUND,
                                                 "read_confirmation")
    assert result.committed_steps == ["confirm"] and not result.retryable


def test_mismatch_after_commit_escalates_instead_of_failing(open_session: OpenSession) -> None:
    cap = with_step(OPEN_SUBACCOUNT, "confirm",
                    expect=Checkpoint(state="member_detail", timeout_ms=800))
    control = InMemoryControl(approve=lambda _: True)  # no operator for the handoff: aborts
    result = replay(cap, PARAMS, open_session(control=control))
    assert isinstance(result, Aborted)
    [request] = control.interventions
    assert request.step_id == "confirm" and "may have taken effect" in request.reason
    assert result.committed_steps == ["confirm"]
