import pytest

from cua.policy import (
    ActionContext,
    Actor,
    Allow,
    Deny,
    Mode,
    Policy,
    PolicyConfig,
    Redactor,
    RequireApproval,
)
from cua.schema import (
    Click,
    ElementInfo,
    Extract,
    Fill,
    Navigate,
    Press,
    Risk,
    Select,
    Status,
)

BASE = "http://bank.test:8001"
SEARCH = ElementInfo(role="button", name="Search", tag="input")
CONFIRM = ElementInfo(role="button", name="Confirm", tag="input")
TEXTBOX = ElementInfo(role="textbox", tag="input", input_type="text")
PASSWORD = ElementInfo(role="textbox", tag="input", input_type="password")


@pytest.fixture
def policy(corebank_policy: PolicyConfig) -> Policy:
    return Policy(corebank_policy, allowed_origins=[BASE])


def ctx(action, element=None, *, mode=Mode.REPLAY, actor=Actor.AGENT,  # type: ignore[no-untyped-def]
        status=Status.APPROVED, declared=None, actions=0, irreversible=0) -> ActionContext:
    return ActionContext(mode=mode, actor=actor, action=action, element=element,
                         declared_risk=declared, capability_status=status,
                         actions_taken=actions, irreversible_taken=irreversible)


@pytest.mark.parametrize(
    ("action", "element", "risk"),
    [
        (Navigate(url=f"{BASE}/inquiry"), None, Risk.READ_ONLY),
        (Fill(target="t", value="12345"), TEXTBOX, Risk.READ_ONLY),
        (Select(target="t", option="Share Savings"), None, Risk.READ_ONLY),
        (Extract(target="t", into="o"), None, Risk.READ_ONLY),
        (Press(key="Tab"), TEXTBOX, Risk.READ_ONLY),
        (Press(key="Enter"), TEXTBOX, Risk.REVERSIBLE),
        (Click(target="t"), SEARCH, Risk.REVERSIBLE),
        (Click(target="t"), CONFIRM, Risk.IRREVERSIBLE),
        (Click(target="t"), ElementInfo(role="button", name="Submit Transfer"), Risk.IRREVERSIBLE),
        (Click(target="t"), ElementInfo(role="link", name="Open Sub-Account"), Risk.REVERSIBLE),
        (Click(target="t"), ElementInfo(role="textbox", name="Postal code"), Risk.REVERSIBLE),
    ],
)
def test_risk_inference(policy: Policy, action, element, risk) -> None:  # type: ignore[no-untyped-def]
    assert policy.infer_risk(action, element) is risk


# (mode, capability status, element) -> expected decision type
@pytest.mark.parametrize(
    ("mode", "status", "element", "expected"),
    [
        (Mode.DISCOVERY, None, TEXTBOX, Allow),
        (Mode.DISCOVERY, None, SEARCH, Allow),
        (Mode.DISCOVERY, None, CONFIRM, RequireApproval),
        (Mode.REPLAY, Status.APPROVED, SEARCH, Allow),
        (Mode.REPLAY, Status.APPROVED, CONFIRM, RequireApproval),
        (Mode.REPLAY, Status.DRAFT, TEXTBOX, Allow),
        (Mode.REPLAY, Status.DRAFT, SEARCH, RequireApproval),
        (Mode.REPLAY, Status.DRAFT, CONFIRM, Deny),
        (Mode.REPLAY, Status.DEPRECATED, CONFIRM, Deny),
    ],
)
def test_decision_table(policy: Policy, mode, status, element, expected) -> None:  # type: ignore[no-untyped-def]
    action = Fill(target="t", value="x") if element is TEXTBOX else Click(target="t")
    assert type(policy.check(ctx(action, element, mode=mode, status=status))) is expected


def test_declared_risk_can_raise_but_never_lower(policy: Policy) -> None:
    lowered = policy.check(ctx(Click(target="t"), CONFIRM, declared=Risk.READ_ONLY))
    assert isinstance(lowered, RequireApproval)
    assert (lowered.risk, lowered.inferred) == (Risk.IRREVERSIBLE, Risk.IRREVERSIBLE)

    raised = policy.check(ctx(Click(target="t"), SEARCH, declared=Risk.IRREVERSIBLE))
    assert isinstance(raised, RequireApproval)
    assert (raised.risk, raised.inferred) == (Risk.IRREVERSIBLE, Risk.REVERSIBLE)


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://evil.test/inquiry", "not in the allowlist"),
        ("https://bank.test:8001/inquiry", "not in the allowlist"),  # scheme is part of origin
        (f"{BASE}/__admin/reset", "is denied"),
        ("/inquiry", "absolute"),
        ("javascript:alert(1)", "absolute"),
    ],
)
def test_navigation_outside_allowlist_is_denied(policy: Policy, url: str, reason: str) -> None:
    for actor in Actor:  # hard limit: applies to every actor and mode
        decision = policy.check(ctx(Navigate(url=url), actor=actor, mode=Mode.DISCOVERY))
        assert isinstance(decision, Deny) and decision.rule == "allowlist"
        assert reason in decision.reason


def test_link_targets_are_checked_before_clicking(policy: Policy) -> None:
    admin = ElementInfo(role="link", name="Faults", href=f"{BASE}/__admin/faults")
    assert isinstance(policy.check(ctx(Click(target="t"), admin)), Deny)
    inquiry = ElementInfo(role="link", name="Member Inquiry", href=f"{BASE}/inquiry")
    assert isinstance(policy.check(ctx(Click(target="t"), inquiry)), Allow)


def test_only_the_runtime_types_credentials(policy: Policy) -> None:
    fill = Fill(target="t", value="hunter2")
    agent = policy.check(ctx(fill, PASSWORD, mode=Mode.DISCOVERY))
    assert isinstance(agent, Deny) and agent.rule == "credential_entry"
    assert isinstance(policy.check(ctx(fill, PASSWORD, actor=Actor.RUNTIME)), Allow)


def test_budgets(policy: Policy) -> None:
    over = policy.check(ctx(Fill(target="t", value="x"), TEXTBOX, actions=200))
    assert isinstance(over, Deny) and over.rule == "action_budget"

    second = policy.check(ctx(Click(target="t"), CONFIRM, irreversible=1))
    assert isinstance(second, Deny) and second.rule == "irreversible_budget"
    # a human can't be approved past the irreversible budget either
    assert isinstance(policy.check(ctx(Click(target="t"), CONFIRM, actor=Actor.HUMAN,
                                       irreversible=1)), Deny)


def test_humans_are_not_gated_by_risk(policy: Policy) -> None:
    decision = policy.check(ctx(Click(target="t"), CONFIRM, actor=Actor.HUMAN,
                                status=Status.DRAFT))
    assert isinstance(decision, Allow) and decision.risk is Risk.IRREVERSIBLE


def test_policy_needs_an_origin() -> None:
    with pytest.raises(ValueError):
        Policy(PolicyConfig(app_id="corebank"), allowed_origins=[])


def test_redactor_scrubs_registered_values_and_backstop_patterns() -> None:
    r = Redactor()
    r.register("12345", "inputs.member_id")
    r.register("demo-only-password", "env:COREBANK_PASSWORD")
    r.register("", "ignored")

    assert r.scrub("member 12345 not found") == "member «inputs.member_id» not found"
    assert r.scrub("id 123456 and A12345") == "id 123456 and A12345"  # whole tokens only
    assert r.scrub("pw=demo-only-password;") == "pw=«env:COREBANK_PASSWORD»;"
    assert r.scrub("SSN 123-45-6789, card 4111 1111 1111 1111") == "SSN «ssn», card «card_number»"
    assert r.scrub("$4,210.37 on 2026-09-23") == "$4,210.37 on 2026-09-23"

    event = {"action": {"value": "12345"}, "urls": ["http://x/?m=12345"], "n": 12345}
    assert r.redact(event) == {
        "action": {"value": "«inputs.member_id»"},
        "urls": ["http://x/?m=«inputs.member_id»"],
        "n": "«inputs.member_id»",  # a sensitive number is still sensitive
    }
    assert r.redact({"n": 7, "ok": True, "none": None}) == {"n": 7, "ok": True, "none": None}
