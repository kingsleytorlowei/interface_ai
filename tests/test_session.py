"""GuardedSession against the live mock bank: the full pipeline, end to end."""

import json

import pytest

from conftest import OpenSession
from cua.control import ApprovalRequest, InMemoryControl
from cua.policy import Mode
from cua.schema import (
    Click,
    Extract,
    Fill,
    Navigate,
    Risk,
    Select,
    Sensitivity,
    Status,
    Target,
)
from cua.schema.targets import ByNear, ByRole, ByTableCell, FrameSelector
from cua.session import (
    ApprovalRejected,
    Command,
    GuardedSession,
    LeaseLost,
    PolicyDenied,
    Ref,
    TargetError,
)
from mock_bank.app import DEMO_PASSWORD, DEMO_USER


def t(*strategies) -> Target:  # type: ignore[no-untyped-def]
    return Target(frame=[FrameSelector(name="content")], strategies=list(strategies))


MEMBER_ID = t(ByNear(text="Member ID", direction="right", role="textbox"))
SEARCH = t(ByRole(role="button", name="Search"))
BALANCE = t(ByTableCell(row_has_text="Share Savings", column="Balance"))
OPEN_SUBACCT = t(ByRole(role="link", name="Open Sub-Account"))
ACCT_TYPE = t(ByNear(text="Account Type", direction="right", role="combobox"))
DEPOSIT = t(ByNear(text="Initial Deposit", direction="right", role="textbox"))
CONTINUE = t(ByRole(role="button", name="Continue"))
CONFIRM = t(ByRole(role="button", name="Confirm"))
MEMBER_INQUIRY = Target(frame=[FrameSelector(name="nav")],
                        strategies=[ByRole(role="link", name="Member Inquiry")])

def events(session: GuardedSession) -> list[dict]:  # type: ignore[type-arg]
    lines = (session.log.dir / "events.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


def kinds(session: GuardedSession) -> list[str]:
    return [e["kind"] for e in events(session)]


def search(session: GuardedSession, member_id: str) -> None:
    session.execute(Command(Fill(target="member_id", value=member_id), MEMBER_ID,
                            sensitivity=Sensitivity.PII, step_id="enter_member"))
    session.execute(Command(Click(target="search"), SEARCH, step_id="search"))


def to_review(session: GuardedSession) -> None:
    search(session, "12345")
    for cmd in [
        Command(Click(target="open"), OPEN_SUBACCT),
        Command(Select(target="type", option="Holiday Club"), ACCT_TYPE),
        Command(Fill(target="deposit", value="25.00"), DEPOSIT),
        Command(Click(target="continue"), CONTINUE),
    ]:
        report = session.execute(cmd)
    assert "Review Sub-Account" in report.after.text


def test_sign_on_and_lookup_leave_a_redacted_trail(open_session: OpenSession) -> None:
    session = open_session()
    search(session, "12345")
    report = session.execute(Command(Extract(target="balance", into="balance"), BALANCE,
                                     sensitivity=Sensitivity.CONFIDENTIAL, step_id="read"))
    assert report.text == "$4,210.37"
    assert report.effective_risk is Risk.READ_ONLY

    trail = (session.log.dir / "events.jsonl").read_text()
    for secret in (DEMO_PASSWORD, DEMO_USER, "12345", "$4,210.37"):
        assert secret not in trail
    assert "«env:COREBANK_PASSWORD»" in trail and "«member_id»" in trail
    assert "«balance»" in trail

    k = kinds(session)
    assert k[:2] == ["session_opened", "lease_acquired"]
    assert "signed_on" in k
    decisions = [e["data"] for e in events(session) if e["kind"] == "policy_decision"]
    assert {d["verdict"] for d in decisions} == {"allow"}
    assert {d["actor"] for d in decisions} == {"runtime", "agent"}


def test_leaving_the_allowlist_is_denied_before_acting(open_session: OpenSession) -> None:
    session = open_session()
    base = session.env["base_url"]
    with pytest.raises(PolicyDenied) as denied:
        session.execute(Command(Navigate(url=f"{base}/__admin/faults")))
    assert denied.value.rule == "allowlist"
    with pytest.raises(PolicyDenied):
        session.execute(Command(Navigate(url="https://example.com/")))
    assert kinds(session).count("action_started") == 4  # the sign-on steps only


def test_irreversible_step_needs_approval_and_rejection_stops_it(open_session: OpenSession) -> None:
    control = InMemoryControl(approve=lambda _: False)
    session = open_session(control=control)
    to_review(session)
    with pytest.raises(ApprovalRejected) as rejected:
        session.execute(Command(Click(target="confirm"), CONFIRM, declared_risk=Risk.READ_ONLY,
                                step_id="confirm"))
    assert rejected.value.category is None  # a human decided; not a failure
    [request] = control.approvals
    assert request.action == 'click button "Confirm"' and request.risk is Risk.IRREVERSIBLE
    assert "risk_mismatch" in kinds(session)  # the artifact under-declared
    assert "Review Sub-Account" in session.observe().text  # nothing was committed


def test_approved_irreversible_step_runs_once(open_session: OpenSession) -> None:
    session = open_session(control=InMemoryControl(approve=lambda _: True))
    to_review(session)
    report = session.execute(Command(Click(target="confirm"), CONFIRM, step_id="confirm"))
    assert "Sub-account opened successfully" in report.after.text
    assert report.effective_risk is Risk.IRREVERSIBLE

    with pytest.raises(PolicyDenied) as budget:  # a second irreversible action in one run
        session.execute(Command(Click(target="inquiry"), MEMBER_INQUIRY,
                                declared_risk=Risk.IRREVERSIBLE))
    assert budget.value.rule == "irreversible_budget"


def test_takeover_during_approval_is_caught_before_acting(open_session: OpenSession) -> None:
    def operator_takes_over(_: ApprovalRequest) -> bool:
        control.acquire("operator:alice")
        return True

    control = InMemoryControl(approve=operator_takes_over)
    session = open_session(control=control)
    to_review(session)
    with pytest.raises(LeaseLost):
        session.execute(Command(Click(target="confirm"), CONFIRM))
    assert "Review Sub-Account" in session.observe().text
    assert kinds(session)[-2:] == ["lease_lost", "observation"]


def test_draft_capability_cannot_commit(open_session: OpenSession) -> None:
    session = open_session(status=Status.DRAFT, control=InMemoryControl(approve=lambda _: True))
    to_review(session)  # reversible clicks are approved by the operator
    with pytest.raises(PolicyDenied) as denied:
        session.execute(Command(Click(target="confirm"), CONFIRM))
    assert denied.value.rule == "unreviewed_capability"


def test_discovery_acts_through_a_verified_synthesized_target(open_session: OpenSession) -> None:
    session = open_session(mode=Mode.DISCOVERY, status=None)
    textbox = next(n for n in session.observe().nodes() if n.role == "textbox" and n.ref)
    assert textbox.ref
    report = session.execute(Command(Fill(target="member_id", value="12345"), Ref(textbox.ref)))
    assert report.target is not None and report.target.strategies[0] == MEMBER_ID.strategies[0]
    # what discovery would record resolves again on its own
    session.execute(Command(Fill(target="member_id", value="99999"), report.target))
    session.execute(Command(Click(target="search"), SEARCH))
    assert "No member found" in session.observe().text


def test_agent_cannot_type_into_password_fields(open_session: OpenSession) -> None:
    session = open_session(mode=Mode.DISCOVERY, status=None, sign_on=False)
    session.execute(Command(Navigate(url=f"{session.env['base_url']}/login")))
    password = Target(strategies=[ByNear(text="Password", direction="right", role="textbox")])
    with pytest.raises(PolicyDenied) as denied:
        session.execute(Command(Fill(target="pw", value="guess"), password))
    assert denied.value.rule == "credential_entry"


def test_resolution_failures_are_typed_and_snapshotted(open_session: OpenSession) -> None:
    session = open_session()
    with pytest.raises(TargetError) as missing:
        session.execute(Command(Click(target="x"), t(ByRole(role="button", name="Transfer")),
                                timeout_ms=300))
    assert missing.value.category == "target_not_found" and missing.value.attempts
    snapshots = list((session.log.dir / "snapshots").iterdir())
    assert {p.suffix for p in snapshots} == {".txt", ".png"}
