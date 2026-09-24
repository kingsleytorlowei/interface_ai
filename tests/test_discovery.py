"""Discovery end to end with a scripted planner (no model): loop, session, recorder, and the
verification replay of what was recorded."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import yaml

from conftest import OpenSession, load_capability
from cua.control import InMemoryControl, Resolution
from cua.discovery import (
    ClaudePlanner,
    Goal,
    RecordingError,
    ScriptedPlanner,
    discover,
    prune_strategies,
)
from cua.discovery.planner import ToolResult, find_ref
from cua.policy import Mode
from cua.replay import replay
from cua.schema import HumanAction, Risk, Status, Success, Target
from cua.schema.targets import ByRole, FrameSelector
from cua.surface.web import WebSurface

GOALS = Path(__file__).parents[1] / "goals"
LOOKUP_GOAL = Goal.model_validate(
    yaml.safe_load((GOALS / "corebank.member.lookup_balance.yaml").read_text()))

FLOW = ["fill_member_id_field", "click_search_button", "extract_member_name",
        "extract_share_savings_balance"]
OUTCOMES = [
    {"code": "member_not_found", "state": "no_results", "description": "No such member"},
    {"code": "access_denied", "state": "access_denied", "description": "Restricted member"},
]


def fill(value: str = "{{inputs.member_id}}") -> tuple[str, dict[str, Any]]:
    return ("fill", {"ref": ("textbox",), "target_name": "member_id_field", "value": value,
                     "intent": "Enter the member number"})


SEARCH = ("click", {"ref": ("button", "Search"), "target_name": "search_button",
                    "intent": "Search for the member"})


def extract(name: str, cell: str) -> tuple[str, dict[str, Any]]:
    return ("extract", {"ref": ("cell", cell), "output_name": name, "intent": f"Read {name}"})


FINISH = ("finish", {"steps": FLOW, "outcomes": OUTCOMES, "notes": "straightforward"})


def discovery_session(open_session: OpenSession, **kw: Any):  # type: ignore[no-untyped-def]
    return open_session(mode=Mode.DISCOVERY, status=None, **kw)


def verify(open_session: OpenSession, capability, params,  # type: ignore[no-untyped-def]
           audits: list[dict[str, list[bool]]] | None = None):
    """What the CLI does next: replay the draft, auto-approving only reversible steps (and,
    given `audits`, checking every strategy of every target it resolves)."""
    control = InMemoryControl(approve=lambda r: r.risk is Risk.REVERSIBLE)
    session = open_session(status=Status.DRAFT, control=control, sign_on=False,
                           audit_targets=audits is not None)
    result = replay(capability, params, session)
    if audits is not None:
        audits.append(session.target_audits)
    return result


def test_discovered_lookup_is_recorded_and_replays(open_session: OpenSession) -> None:
    session = discovery_session(open_session)
    planner = ScriptedPlanner([fill(), SEARCH, extract("member_name", "Jane Q. Sample"),
                               extract("share_savings_balance", "$4,210.37"), FINISH])
    result = discover(LOOKUP_GOAL, session, planner)

    assert result.status == "recorded", result
    cap = result.capability
    assert cap is not None and cap.status == "draft" and cap.provenance.model == "scripted"
    assert [s.id for s in cap.steps] == FLOW
    assert [s.expect.state if s.expect else None for s in cap.steps] == [
        "member_search", "member_detail", "member_detail", "member_detail"]
    assert cap.steps[0].action.value == "{{inputs.member_id}}"  # type: ignore[union-attr]
    assert cap.steps[1].risk is Risk.REVERSIBLE  # as enforced; a reviewer may lower it
    assert cap.success.state == "member_detail"
    assert set(cap.outcomes) == {"member_not_found", "access_denied"}
    assert any("reversible" in n for n in result.review_notes)

    # what the model was told after the search
    assert "Recorded as step `click_search_button`" in planner.received[1].content

    trail = "".join((session.log.dir / f).read_text()
                    for f in ("events.jsonl", "transcript.json", "capability.draft.json"))
    for sensitive in ("12345", "Jane Q. Sample", "4,210.37"):
        assert sensitive not in trail

    verified = verify(open_session, cap, {"member_id": "12345"})
    assert isinstance(verified, Success), verified
    assert verified.outputs == {"member_name": "Jane Q. Sample",
                                "share_savings_balance": Decimal("4210.37")}
    # the recorded artifact generalises: another member, same flow
    other = verify(open_session, cap, {"member_id": "23456"})
    assert other.kind == "aborted"  # member alert, and nobody to hand it to



def test_the_model_is_only_offered_and_allowed_what_the_policy_permits(
        open_session: OpenSession) -> None:
    session = discovery_session(open_session)
    press = ("press", {"ref": ("textbox",), "target_name": "member_id_field", "key": "Enter",
                       "intent": "Submit the search"})
    sign_off = ("click", {"ref": ("link", "Sign Off"), "target_name": "sign_off",
                          "intent": "Sign off"})
    planner = ScriptedPlanner([fill(), press, sign_off, SEARCH,
                               extract("member_name", "Jane Q. Sample"),
                               extract("share_savings_balance", "$4,210.37"), FINISH])
    result = discover(LOOKUP_GOAL, session, planner)

    assert "press" not in {t["name"] for t in planner.tools}  # corebank policy leaves it out
    refused_press, refused_link = planner.received[1:3]
    assert refused_press.is_error and "press actions are not allowed" in refused_press.content
    assert refused_link.is_error and "/signoff is not in the allowlist" in refused_link.content
    assert result.status == "recorded" and result.capability is not None
    assert [s.id for s in result.capability.steps] == FLOW  # refusals never become steps


def test_literal_example_values_are_parameterised(open_session: OpenSession) -> None:
    planner = ScriptedPlanner([fill("12345"), SEARCH, extract("member_name", "Jane Q. Sample"),
                               extract("share_savings_balance", "$4,210.37"), FINISH])
    result = discover(LOOKUP_GOAL, discovery_session(open_session), planner)
    assert result.capability is not None
    assert result.capability.steps[0].action.value == "{{inputs.member_id}}"  # type: ignore[union-attr]
    assert "recorded as a placeholder" in result.review_notes[0]


def test_interstitials_are_handled_but_not_recorded(open_session: OpenSession) -> None:
    session = discovery_session(open_session)
    httpx.put(f"{session.env['base_url']}/__admin/faults",
              json={"broadcast_notices": 1}).raise_for_status()
    dismiss = ("click", {"ref": ("button", "Continue"), "target_name": "continue_button",
                         "intent": "Dismiss the notice"})
    planner = ScriptedPlanner([fill(), SEARCH, dismiss, extract("member_name", "Jane Q. Sample"),
                               extract("share_savings_balance", "$4,210.37"), FINISH])
    result = discover(LOOKUP_GOAL, session, planner)
    assert "not recorded: system_notice" in planner.received[2].content
    assert result.capability is not None
    search = result.capability.steps[1]
    assert search.expect and search.expect.state == "member_detail"  # past the notice
    assert "continue_button" not in result.capability.targets


ACKNOWLEDGE = Target(frame=[FrameSelector(name="content")],
                     strategies=[ByRole(role="button", name="Acknowledge")])


def test_member_alert_needs_a_person(open_session: OpenSession, browser: WebSurface) -> None:
    def operator(_: object) -> Resolution:
        control.acquire("operator:alice")
        browser.click(browser.resolve(ACKNOWLEDGE))
        browser.settle()
        return Resolution("resumed", "alice", (HumanAction(
            kind="click", target_description='button "Acknowledge"', at=datetime.now(UTC)),))

    control = InMemoryControl(intervene=operator)
    goal = LOOKUP_GOAL.model_copy(update={"inputs": {"member_id": LOOKUP_GOAL.inputs[
        "member_id"].model_copy(update={"example": "23456"})}})
    self_ack = ("click", {"ref": ("button", "Acknowledge"), "target_name": "ack",
                          "intent": "Acknowledge the alert"})
    planner = ScriptedPlanner([
        fill(), SEARCH, self_ack, ("request_help", {"reason": "member alert"}),
        extract("member_name", "Robert T. Example"),
        extract("share_savings_balance", "$815.00"), FINISH])
    result = discover(goal, discovery_session(open_session, control=control), planner)

    assert planner.received[2].is_error and "needs a person" in planner.received[2].content
    assert "handed control back" in planner.received[3].content
    assert result.status == "recorded" and result.capability is not None
    assert result.capability.steps[1].expect.state == "member_detail"  # type: ignore[union-attr]


def test_problems_are_reported_back_to_the_model(open_session: OpenSession) -> None:
    planner = ScriptedPlanner([
        ("click", {"ref": "nope", "target_name": "x", "intent": "bad ref"}),
        fill(), SEARCH,
        ("finish", {"steps": FLOW[:2], "outcomes": [], "notes": ""}),
        ("give_up", {"reason": "cannot find the balance"}),
    ])
    result = discover(LOOKUP_GOAL, discovery_session(open_session), planner)
    bad_ref, finish = planner.received[0], planner.received[3]
    assert bad_ref.is_error and "Not done" in bad_ref.content and "Screen:" in bad_ref.content
    assert finish.is_error and "goal outputs never extracted" in finish.content
    assert (result.status, result.reason) == ("gave_up", "cannot find the balance")


def test_outcome_on_the_success_path_is_sent_back(open_session: OpenSession) -> None:
    """Seen in the first real run: the model mapped outcomes onto member_search and
    member_detail, which ended verification on the search form. It must fix that first."""
    on_path = [*OUTCOMES, {"code": "system_error", "state": "member_search",
                           "description": "Error during search"}]
    planner = ScriptedPlanner([
        fill(), SEARCH, extract("member_name", "Jane Q. Sample"),
        extract("share_savings_balance", "$4,210.37"),
        ("finish", {"steps": FLOW, "outcomes": on_path, "notes": ""}), FINISH])
    result = discover(LOOKUP_GOAL, discovery_session(open_session), planner)
    rejected = planner.received[4]
    assert rejected.is_error and "on the success path" in rejected.content
    assert result.status == "recorded" and result.capability is not None
    assert set(result.capability.outcomes) == {"member_not_found", "access_denied"}


def test_turn_budget_and_silent_stops(open_session: OpenSession) -> None:
    goal = LOOKUP_GOAL.model_copy(update={"max_turns": 2})
    result = discover(goal, discovery_session(open_session),
                      ScriptedPlanner([fill(), fill(), fill()]))
    assert result.status == "budget_exhausted"

    silent = discover(LOOKUP_GOAL, discovery_session(open_session), ScriptedPlanner([]))
    assert (silent.status, silent.reason) == ("gave_up", "the model stopped without finishing")


def test_find_ref() -> None:
    snap = '- textbox [ref=f4e2]\n- button "Search" [ref=f4e3] [cursor=pointer]\n' \
           '- cell "Jane \\"J\\" Q." [ref=f4e9]'
    assert find_ref(snap, "textbox") == "f4e2"
    assert find_ref(snap, "button", "Search") == "f4e3"
    with pytest.raises(LookupError):
        find_ref(snap, "button", "Find")


# --- the Claude planner's request shape (no network) ----------------------------------------


class FakeMessages:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.requests.append(json.loads(json.dumps(kwargs, default=str)))
        return SimpleNamespace(
            content=[SimpleNamespace(type="thinking", thinking=""),
                     SimpleNamespace(type="tool_use", id="toolu_1", name="click",
                                     input={"ref": "f1", "target_name": "go", "intent": "go"})],
            stop_reason="tool_use",
            usage=SimpleNamespace(input_tokens=1200, output_tokens=80,
                                  cache_read_input_tokens=1000, cache_creation_input_tokens=0),
        )


def test_claude_planner_request_shape() -> None:
    fake = FakeMessages()
    planner = ClaudePlanner(SimpleNamespace(beta=SimpleNamespace(messages=fake)))
    tools = [{"name": "click", "strict": True, "input_schema": {}}]
    turn = planner.begin("system prompt", tools, "task")
    assert turn.calls[0].name == "click" and turn.usage["cache_read_input_tokens"] == 1000
    planner.respond([ToolResult("toolu_1", "Done.")])

    first, second = fake.requests
    assert first["model"] == "claude-opus-5"
    assert first["thinking"] == {"type": "adaptive"}
    assert first["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
    assert first["fallbacks"] == "default"
    assert set(first["betas"]) == {"context-management-2025-06-27",
                                   "server-side-fallback-2026-07-01"}
    assert first["context_management"]["edits"][0]["type"] == "clear_tool_uses_20250919"
    assert first["cache_control"] == {"type": "ephemeral"}
    # append-only history: the second request extends the first
    assert len(second["messages"]) == 3
    assert second["messages"][2]["content"][0]["tool_use_id"] == "toolu_1"


PREPARE_GOAL = Goal.model_validate(
    yaml.safe_load((GOALS / "corebank.subaccount.prepare.yaml").read_text()))
PREPARE_FLOW = ["fill_member_id_field", "click_search_button", "click_open_subaccount_link",
                "select_account_type_select", "fill_initial_deposit_field",
                "click_continue_button",
                "extract_account_type", "extract_initial_deposit"]


def test_discovered_subaccount_flow_stops_before_the_commit(open_session: OpenSession) -> None:
    def act(verb: str, ref: tuple[Any, ...], name: str, **args: str) -> tuple[str, dict[str, Any]]:
        return (verb, {"ref": ref, "target_name": name, "intent": name, **args})

    confirm = act("click", ("button", "Confirm"), "confirm_button")
    planner = ScriptedPlanner([
        fill(), SEARCH,
        act("click", ("link", "Open Sub-Account"), "open_subaccount_link"),
        act("select", ("combobox",), "account_type_select", option="{{inputs.account_type}}"),
        act("fill", ("textbox", None, 1), "initial_deposit_field",
            value="{{inputs.initial_deposit}}"),
        act("click", ("button", "Continue"), "continue_button"),
        confirm,  # the policy stops this in discovery: irreversible, and nobody approves
        ("extract", {"ref": ("cell", "Holiday Club"), "output_name": "account_type",
                     "intent": "Read the account type"}),
        ("extract", {"ref": ("cell", "$25.00"), "output_name": "initial_deposit",
                     "intent": "Read the initial deposit"}),
        ("finish", {"steps": PREPARE_FLOW, "notes": "stops on review", "outcomes": [
            {"code": "member_not_found", "state": "no_results", "description": "No member"},
            {"code": "deposit_rejected", "state": "subacct_invalid",
             "description": "Deposit or account type rejected"}]}),
    ])
    session = discovery_session(open_session)  # unattended: approvals are rejected
    result = discover(PREPARE_GOAL, session, planner)

    refused = planner.received[6]
    assert refused.is_error and "approval rejected" in refused.content
    assert result.status == "recorded", result
    cap = result.capability
    assert cap is not None and [s.id for s in cap.steps] == PREPARE_FLOW
    assert cap.success.state == "subacct_review"
    assert cap.risk is Risk.REVERSIBLE  # nothing irreversible was recorded

    # As in the real run: a fallback for Continue anchored on the heading, which holds the
    # member's name (never declared sensitive, so redaction can't know it). The transcript
    # evidence keeps no screen trees, so the name isn't there either.
    assert "Jane Q. Sample" in cap.model_dump_json()
    transcript = (session.log.dir / "transcript.json").read_text()
    assert "Jane Q. Sample" not in transcript and "screen tree omitted" in transcript

    # Verification: both example sets, auditing every strategy; then prune.
    audits: list[dict[str, list[bool]]] = []
    [first, second] = PREPARE_GOAL.verification_params()
    assert second["member_id"] == "45678"
    for params in (first, second):
        verified = verify(open_session, cap, params, audits)
        assert isinstance(verified, Success), verified
    assert verified.outputs == {"account_type": "Money Market",
                                "initial_deposit": Decimal("40.00")}
    assert verified.committed_steps != []  # reversible steps count; a restart would redo them
    pruned, notes = prune_strategies(cap, audits)
    assert "Jane Q. Sample" not in pruned.model_dump_json()
    assert any(n.startswith("target continue_button: dropped strategy #2") for n in notes)
    assert all("Jane" not in n for n in notes)
    assert pruned.targets["continue_button"].strategies == \
        cap.targets["continue_button"].strategies[:1]

    params = first
    rejected = verify(open_session, cap, {**params, "initial_deposit": "1.00"})
    assert rejected.kind == "business_outcome" and rejected.code == "deposit_rejected"


def test_pruning_needs_one_strategy_that_held_in_every_run() -> None:
    cap = load_capability("corebank.member.lookup_balance")
    two = next(n for n, t in cap.targets.items() if len(t.strategies) == 2)
    others = {n: [True] * len(t.strategies) for n, t in cap.targets.items() if n != two}
    # each run won with a different strategy: neither is proven for both inputs
    with pytest.raises(RecordingError, match=f"no strategy of target {two}"):
        prune_strategies(cap, [{**others, two: [True, False]}, {**others, two: [False, True]}])
    # a target no run resolved is kept, flagged as unproven
    kept, notes = prune_strategies(cap, [others])
    assert kept.targets[two] == cap.targets[two]
    assert notes == [f"target {two} was never resolved during verification; its 2 "
                     "strategies are unproven"]


def test_verification_params_add_an_alternate_set_only_when_declared() -> None:
    assert PREPARE_GOAL.verification_params()[1] == {
        "member_id": "45678", "account_type": "Money Market", "initial_deposit": "40.00"}
    single = LOOKUP_GOAL.model_copy(update={"inputs": {
        n: s.model_copy(update={"alt_example": None}) for n, s in LOOKUP_GOAL.inputs.items()}})
    assert single.verification_params() == [{"member_id": "12345"}]
