"""The operator desk (threads), the console API, capture of human actions, and pause."""

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from conftest import OpenSession, load_capability
from cua.control import (
    ApprovalRequest,
    DeskError,
    InMemoryControl,
    InterventionRequest,
    OperatorDesk,
    Resolution,
)
from cua.operator import create_console
from cua.replay import replay
from cua.schema import Aborted, Risk, Success, Target
from cua.schema.targets import ByNear, ByRole, FrameSelector
from cua.surface.web import WebSurface

APPROVAL = ApprovalRequest("r1", "confirm", 'click button "Confirm"', Risk.IRREVERSIBLE,
                           "irreversible", id="r1-ap1", evidence_ref="r1/snapshots/0001-approval")
HANDOFF = InterventionRequest("r1-iv1", "r1", "search", "member alert")


def in_thread(fn: Callable[[], Any]) -> tuple[threading.Thread, list[Any]]:
    out: list[Any] = []
    thread = threading.Thread(target=lambda: out.append(fn()), daemon=True)
    thread.start()
    return thread, out


def wait_for_pending(desk: OperatorDesk, n: int = 1) -> list[dict[str, Any]]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        pending = [i for i in desk.state()["items"] if i["status"] == "pending"]
        if len(pending) >= n:
            return pending
        time.sleep(0.01)
    raise AssertionError("request never reached the desk")


# --- desk -----------------------------------------------------------------------------------


def test_desk_blocks_until_an_operator_decides() -> None:
    desk = OperatorDesk()
    thread, out = in_thread(lambda: desk.request_approval(APPROVAL, timeout_s=5))
    [item] = wait_for_pending(desk)
    assert (item["kind"], item["risk"], item["action"]) == (
        "approval", "irreversible", 'click button "Confirm"')
    desk.resolve_approval("r1-ap1", True, "alice")
    thread.join(2)
    assert out == [True]
    with pytest.raises(DeskError, match="already approved"):
        desk.resolve_approval("r1-ap1", False, "bob")


def test_desk_fails_closed_on_silence() -> None:
    desk = OperatorDesk()
    assert desk.request_approval(APPROVAL, timeout_s=0.05) is False
    resolution = desk.request_intervention(HANDOFF, timeout_s=0.05)
    assert (resolution.outcome, resolution.note) == ("aborted", "no operator responded in time")
    assert {i["status"] for i in desk.state()["items"]} == {"expired"}


def test_desk_hands_back_with_a_note_and_needs_a_name() -> None:
    desk = OperatorDesk()
    thread, out = in_thread(lambda: desk.request_intervention(HANDOFF, timeout_s=5))
    wait_for_pending(desk)
    with pytest.raises(DeskError, match="operator name"):
        desk.resolve_intervention("r1-iv1", "resumed", "  ")
    with pytest.raises(DeskError, match="no approval"):
        desk.resolve_approval("r1-iv1", True, "alice")
    desk.resolve_intervention("r1-iv1", "resumed", "alice", "verified ID")
    thread.join(2)
    assert out == [Resolution("resumed", "alice", note="verified ID")]


def test_takeover_and_pause() -> None:
    desk = OperatorDesk()
    agent = desk.acquire("agent:r1")
    desk.take_over("alice")
    assert not desk.is_current(agent) and desk.state()["holder"] == "operator:alice"
    desk.pause()
    assert desk.state()["pause_requested"]
    assert desk.consume_pause() is True and desk.consume_pause() is False


# --- console API ----------------------------------------------------------------------------


def test_console_api(tmp_path: Path) -> None:
    (tmp_path / "r1" / "snapshots").mkdir(parents=True)
    (tmp_path / "r1" / "snapshots" / "0001-approval.png").write_bytes(b"png")
    (tmp_path / "secret.env").write_text("x")
    desk = OperatorDesk()
    client = TestClient(create_console(desk, tmp_path))

    assert "Operator console" in client.get("/").text
    thread, out = in_thread(lambda: desk.request_approval(APPROVAL, timeout_s=5))
    wait_for_pending(desk)
    [item] = client.get("/api/state").json()["items"]
    assert client.get(f"/evidence/{item['evidence_ref']}.png").content == b"png"
    for bad in ("../secret.env", "r1/../secret.env", "secret.env"):
        assert client.get(f"/evidence/{bad}").status_code == 404

    assert client.post("/api/approvals/r1-ap1",
                       json={"approve": False, "operator": ""}).status_code == 409
    assert client.post("/api/approvals/r1-ap1",
                       json={"approve": False, "operator": "alice"}).status_code == 200
    thread.join(2)
    assert out == [False]
    assert client.post("/api/approvals/r1-ap1",
                       json={"approve": True, "operator": "alice"}).status_code == 409

    assert client.post("/api/pause", json={"operator": "alice"}).status_code == 200
    assert client.post("/api/takeover", json={"operator": "alice"}).status_code == 200
    assert client.get("/api/state").json()["holder"] == "operator:alice"


# --- capture and pause on the live app ------------------------------------------------------

CONTENT = [FrameSelector(name="content")]
MEMBER_FIELD = Target(frame=CONTENT, strategies=[
    ByNear(text="Member ID", direction="right", role="textbox")])
SEARCH = Target(frame=CONTENT, strategies=[ByRole(role="button", name="Search")])


def test_human_actions_are_captured_and_typed_text_is_not_kept(
    open_session: OpenSession, browser: WebSurface
) -> None:
    def human(_: InterventionRequest) -> Resolution:
        browser.fill(browser.resolve(MEMBER_FIELD), "99999")  # stands in for a person
        browser.click(browser.resolve(SEARCH))
        browser.settle()
        return Resolution("resumed", "alice", note="looked it up by hand")

    session = open_session(control=InMemoryControl(intervene=human))
    browser.click(browser.resolve(SEARCH))  # the engine's own clicks are never captured...
    browser.settle()
    intervention = session.request_intervention("check something")
    browser.click(browser.resolve(SEARCH))  # ...nor anything after the handoff
    browser.settle()
    assert [(a.kind, a.target_description, a.value) for a in intervention.actions] == [
        ("fill", 'textbox "mbrno"', "«5 characters»"),
        ("click", 'button "Search"', None),
    ]
    assert session.interventions == [intervention]


LOOKUP = load_capability("corebank.member.lookup_balance")


@pytest.mark.parametrize("outcome", ["resumed", "aborted"])
def test_operator_pause_hands_over_mid_run(open_session: OpenSession, outcome: str) -> None:
    control = InMemoryControl(intervene=lambda _: Resolution(outcome, "alice"))  # type: ignore[arg-type]
    session = open_session(control=control)
    control.pause()
    result = replay(LOOKUP, {"member_id": "12345"}, session)
    [request] = control.interventions
    assert request.reason == "an operator asked to pause" and request.evidence_ref
    assert (session.log.dir.parent / f"{request.evidence_ref}.png").exists()
    if outcome == "resumed":
        assert isinstance(result, Success)
        assert [i.reason for i in result.interventions] == ["an operator asked to pause"]
    else:
        assert isinstance(result, Aborted) and "while paused" in result.reason
