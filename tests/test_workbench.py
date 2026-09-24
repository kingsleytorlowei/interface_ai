"""The operator workbench: what a bank employee sees and can decide, the plain-language
results, one job at a time, and a real run through it against the mock bank."""

import re
import shutil
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from conftest import CATALOG, load_capability
from cua import workflows
from cua.control import OperatorDesk
from cua.discovery import (
    ClaudeContractProposer,
    ContractProposal,
    ProposalError,
    ScriptedPlanner,
    sensitive_looking,
)
from cua.discovery.contract import to_proposal
from cua.operator import present
from cua.operator.jobs import JobRunner
from cua.operator.workbench import WorkbenchConfig, create_workbench
from cua.schema import (
    BusinessOutcome,
    Capability,
    Failure,
    FailureCategory,
    InputSpec,
    OutputSpec,
    RunResult,
    Status,
    Success,
)
from cua.store import Store
from mock_bank.app import DEMO_PASSWORD, DEMO_USER
from test_discovery import FINISH, SEARCH, extract, fill
from test_store import draft

LOOKUP = load_capability("corebank.member.lookup_balance")
NOW = datetime.now(UTC)


def text(html: str) -> str:
    """What a person reads: tags, styles and scripts removed."""
    html = re.sub(r"<(style|script)>.*?</\1>", "", html, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).replace("&#34;", '"')


@pytest.fixture
def store(tmp_path: Path) -> Store:
    shutil.copytree(CATALOG / "corebank", tmp_path / "catalog" / "corebank",
                    ignore=shutil.ignore_patterns("capabilities", "tenants"))
    return Store(tmp_path / "catalog")


@pytest.fixture
def jobs() -> JobRunner:
    return JobRunner()


@pytest.fixture
def client(store: Store, jobs: JobRunner, tmp_path: Path) -> TestClient:
    config = WorkbenchConfig(store, tmp_path / "evidence", "http://127.0.0.1:1", headed=False)
    client = TestClient(create_workbench(config, OperatorDesk(), jobs))
    client.cookies.set("operator", "Alice Reviewer")
    return client


def verified_draft(store: Store) -> Capability:
    cap = draft()
    store.save(cap, {"kind": "success", "runs": [], "review_notes": [
        "step search is recorded as reversible; lower it to read_only at review if it only "
        "queries"]})
    return cap


# --- review and approve ---------------------------------------------------------------------


def test_a_draft_is_reviewed_in_plain_words(store: Store, client: TestClient) -> None:
    cap = verified_draft(store)
    listing = text(client.get("/automations").text)
    assert "ready for review" in listing
    page = text(client.get(f"/automations/{cap.id}/{cap.version}").text)
    assert 'text box right of "Member ID"' in page
    assert 'cell in row "Share Savings", column "Balance"' in page
    assert "may change data" in page and "treat as \"reads only\"" in page
    assert "recorded as reversible" in page  # the discovery note reaches the reviewer
    assert "mbrno" not in page and "strategies" not in page  # no locators, no JSON


def test_approving_from_the_page(store: Store, client: TestClient) -> None:
    cap = verified_draft(store)
    url = f"/automations/{cap.id}/{cap.version}"
    response = client.post(f"{url}/approve", data={"reviewer": "", "read_only": ["search"]})
    assert "reviewer" in text(response.text)  # a name is required
    response = client.post(f"{url}/approve",
                           data={"reviewer": "Alice Reviewer", "read_only": ["search"]})
    assert "Approved" in text(response.text)
    approved = store.load(cap.id, cap.version)
    assert approved.status is Status.APPROVED
    assert approved.provenance.reviewed_by == "Alice Reviewer"
    assert next(s for s in approved.steps if s.id == "search").risk == "read_only"


def test_rejecting_from_the_page(store: Store, client: TestClient) -> None:
    cap = verified_draft(store)
    url = f"/automations/{cap.id}/{cap.version}"
    response = client.post(f"{url}/reject",
                           data={"reviewer": "Alice Reviewer", "reason": "reads the wrong row"})
    page = text(response.text)
    assert "Rejected by Alice Reviewer: reads the wrong row" in page
    assert "Approve" not in page
    assert "rejected" in text(client.get("/automations").text)
    response = client.post(f"{url}/approve", data={"reviewer": "Alice Reviewer"})
    assert "was rejected by Alice Reviewer" in text(response.text)


# --- run ------------------------------------------------------------------------------------


def run_through(client: TestClient, jobs: JobRunner, capability_id: str,
                inputs: dict[str, str]) -> str:
    response = client.post(f"/run/{capability_id}",
                           data={f"in_{k}": v for k, v in inputs.items()},
                           follow_redirects=False)
    assert response.status_code == 303, text(response.text)
    job_id = response.headers["location"].rsplit("/", 1)[1]
    jobs.wait(job_id)
    return text(client.get(f"/jobs/{job_id}").text)


def test_the_run_form_is_built_from_the_inputs(store: Store, client: TestClient) -> None:
    store.save(LOOKUP)
    html = client.get(f"/run/{LOOKUP.id}").text
    assert 'name="in_member_id"' in html and 'pattern="\\d{5}"' in html
    assert "personal data" in text(html)


def test_a_run_reports_in_plain_words(store: Store, client: TestClient, jobs: JobRunner,
                                      monkeypatch: pytest.MonkeyPatch) -> None:
    store.save(LOOKUP)
    seen: dict[str, Any] = {}

    def fake_replay(ws: Any, capability: Capability, inputs: dict[str, str], *,
                    control: Any, attended: bool, headed: bool = False) -> RunResult:
        seen.update(inputs=inputs, attended=attended, control=control)
        return Success(run_id="r", capability_id=capability.id,
                       capability_version=capability.version, started_at=NOW, finished_at=NOW,
                       evidence_ref="evidence/r",
                       outputs={"member_name": "Jane Q. Sample",
                                "share_savings_balance": Decimal("4210.37")})

    monkeypatch.setattr(workflows, "replay_capability", fake_replay)
    page = run_through(client, jobs, LOOKUP.id, {"member_id": "12345"})
    assert "Done" in page and "$4,210.37" in page and "Jane Q. Sample" in page
    # a person started it, so approvals and handoffs go to them, not fail closed
    assert seen["inputs"] == {"member_id": "12345"} and seen["attended"] is True
    assert isinstance(seen["control"], OperatorDesk)


def test_one_job_at_a_time(store: Store, client: TestClient, jobs: JobRunner) -> None:
    store.save(LOOKUP)
    release = threading.Event()
    jobs.submit("run", "something slow", lambda progress: release.wait(5))
    response = client.post(f"/run/{LOOKUP.id}", data={"in_member_id": "12345"})
    assert "busy: something slow" in text(response.text)
    release.set()


def test_needs_you_shows_the_desk_with_the_navigation(client: TestClient) -> None:
    page = client.get("/needs-you").text
    assert "Operator workbench" in page and "/api/state" in page


# --- plain-language results -----------------------------------------------------------------


def failure(category: FailureCategory, *, step_id: str = "search", retryable: bool = True,
            committed: list[str] | None = None, **fields: Any) -> Failure:
    return Failure(run_id="r", capability_id=LOOKUP.id, capability_version=LOOKUP.version,
                   started_at=NOW, finished_at=NOW, evidence_ref="e", category=category,
                   step_id=step_id, expected=fields.get("expected", "member_detail"),
                   observed=fields.get("observed", "-"), retryable=retryable,
                   message=fields.get("message", "m"), committed_steps=committed or [])


@pytest.mark.parametrize("result,headline,advice", [
    (failure(FailureCategory.TIMEOUT), "didn't respond in time at \"Search for the member\"",
     "Nothing was changed; safe to try again."),
    (failure(FailureCategory.TARGET_NOT_FOUND, retryable=False),
     "Couldn't find the button \"Search\"", "Nothing was changed."),
    (failure(FailureCategory.CHECKPOINT_MISMATCH, observed="no_results", retryable=False,
             committed=["search"]),
     "Expected the member detail screen", "May already have taken effect: Search for the "
     "member. Check before running it again."),
])
def test_failures_say_what_happened_and_what_to_do(result: Failure, headline: str,
                                                   advice: str) -> None:
    view = present.result(LOOKUP, result)
    assert headline in view.headline and view.advice == advice
    assert result.category.value not in view.headline  # no codes


def test_a_business_outcome_uses_the_capabilitys_own_words() -> None:
    code, spec = next(iter(LOOKUP.outcomes.items()))
    view = present.result(LOOKUP, BusinessOutcome(
        run_id="r", capability_id=LOOKUP.id, capability_version=LOOKUP.version,
        started_at=NOW, finished_at=NOW, evidence_ref="e", code=code, message="x"))
    assert (view.tone, view.headline) == ("outcome", spec.description)


# --- live -----------------------------------------------------------------------------------


def test_a_real_run_through_the_workbench(tmp_path: Path, store: Store, jobs: JobRunner,
                                          bank: Callable[[str], str],
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COREBANK_USER", DEMO_USER)
    monkeypatch.setenv("COREBANK_PASSWORD", DEMO_PASSWORD)
    store.save(LOOKUP)
    config = WorkbenchConfig(store, tmp_path / "evidence", bank("pinnacle"), headed=False)
    client = TestClient(create_workbench(config, OperatorDesk(), jobs))
    page = run_through(client, jobs, LOOKUP.id, {"member_id": "12345"})
    assert "Done" in page and "Jane Q. Sample" in page and "$4,210.37" in page


def test_old_single_run_verification_records_still_read(store: Store,
                                                        client: TestClient) -> None:
    store.save(LOOKUP, {"kind": "success", "run_id": "r1", "evidence_ref": "evidence/r1"})
    assert "Replayed 1 time without" in text(
        client.get(f"/automations/{LOOKUP.id}/{LOOKUP.version}").text)


# --- new automation -------------------------------------------------------------------------


def test_requests_with_member_data_are_never_sent() -> None:
    proposer = ClaudeContractProposer(client=object())  # would fail if it were called
    with pytest.raises(ProposalError, match="12345"):
        proposer("Look up member 12345's balance", "corebank", "", [])
    assert sensitive_looking("card 4111 1111 1111 1111, jane@example.com") == [
        "1111", "4111", "4111 1111 1111 1111", "jane@example.com"]


def test_a_proposal_is_cleaned_of_values_and_bad_rules() -> None:
    proposal = to_proposal({
        "title": "Open an account", "name": "corebank.subaccount.open_thing",
        "inputs": [{"name": "Member ID", "type": "string", "description": "the member",
                    "sensitivity": "pii", "pattern": "([", "choices": None},
                   {"name": "product", "type": "string", "description": "which product",
                    "sensitivity": "secret", "pattern": None,
                    "choices": ["Holiday Club", "98765"]}],
        "outputs": [{"name": "confirmation", "type": "string", "description": "number",
                     "sensitivity": "internal"}],
        "questions": ["one or many?"]}, "corebank", "open one for member 98765")
    assert proposal.capability_id == "corebank.subaccount.open_thing"
    assert list(proposal.inputs) == ["member_id", "product"]
    assert proposal.inputs["member_id"].pattern is None  # "([" isn't a valid pattern
    assert proposal.inputs["product"].enum == ["Holiday Club"]  # the request's value removed
    assert proposal.inputs["product"].sensitivity == "confidential"  # never "secret"
    assert any("format rule" in w for w in proposal.warnings)


def fake_proposer(proposal: ContractProposal) -> Callable[..., ContractProposal]:
    return lambda request, app_id, description, examples: proposal


PROPOSAL = ContractProposal(
    title="Member balance", capability_id="corebank.member.lookup_balance",
    inputs={"member_id": InputSpec(type="string", description="5-digit member number",
                                   sensitivity="pii", pattern=r"^\d{5}$")},
    outputs={"member_name": OutputSpec(type="string", description="The member's name",
                                       sensitivity="pii"),
             "share_savings_balance": OutputSpec(type="money", description="Savings balance",
                                                 sensitivity="confidential")},
    questions=["Always exactly 5 digits?"])


def contract_form(**overrides: str) -> dict[str, str]:
    form = {"request": "Look up a member's name and savings balance by member number",
            "title": "Member balance", "capability_id": "corebank.member.lookup_balance",
            "incount": "1", "in0_name": "member_id", "in0_type": "string",
            "in0_description": "5-digit member number", "in0_sensitivity": "pii",
            "in0_pattern": r"^\d{5}$", "in0_example": "12345", "in0_alt_example": "45678",
            "outcount": "2", "out0_name": "member_name", "out0_type": "string",
            "out0_description": "The member's full name", "out0_sensitivity": "pii",
            "out1_name": "share_savings_balance", "out1_type": "money",
            "out1_description": "Balance of the member's Share Savings account",
            "out1_sensitivity": "confidential"}
    return {**form, **overrides}


def test_the_proposal_is_shown_for_the_person_to_correct(store: Store, tmp_path: Path) -> None:
    config = WorkbenchConfig(store, tmp_path / "evidence", "http://127.0.0.1:1", headed=False,
                             proposer=fake_proposer(PROPOSAL), planner=lambda: ScriptedPlanner([]))
    client = TestClient(create_workbench(config, OperatorDesk(), JobRunner()))
    page = client.post("/new", data={"request_text": "Look up a member's balance"}).text
    assert 'value="member_id"' in page and 'value="share_savings_balance"' in page
    assert "Always exactly 5 digits?" in text(page)
    # a missing example is sent back with everything the person typed kept
    page = client.post("/new/discover", data=contract_form(in0_example="")).text
    assert "Give an example value for member_id" in text(page)
    assert 'value="45678"' in page


def test_a_new_automation_found_through_the_workbench(
        tmp_path: Path, store: Store, jobs: JobRunner, bank: Callable[[str], str],
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Request → contract → discovery (scripted, no model) → two checking replays → a draft
    ready for review, with the progress narrated in plain words."""
    monkeypatch.setenv("COREBANK_USER", DEMO_USER)
    monkeypatch.setenv("COREBANK_PASSWORD", DEMO_PASSWORD)
    script = [fill(), SEARCH, extract("member_name", "Jane Q. Sample"),
              extract("share_savings_balance", "$4,210.37"), FINISH]
    config = WorkbenchConfig(store, tmp_path / "evidence", bank("pinnacle"), headed=False,
                             proposer=fake_proposer(PROPOSAL),
                             planner=lambda: ScriptedPlanner(script))
    client = TestClient(create_workbench(config, OperatorDesk(), jobs))
    response = client.post("/new/discover", data=contract_form(), follow_redirects=False)
    assert response.status_code == 303, text(response.text)
    job_id = response.headers["location"].rsplit("/", 1)[1]
    job = jobs.wait(job_id, timeout_s=120)
    page = text(client.get(f"/jobs/{job_id}").text)
    assert job.status == "done", job.error
    assert "a draft is ready for review" in page
    assert 'Filled the text box right of "Member ID"' in page
    assert 'Clicked the button "Search"' in page
    draft_ = store.load(PROPOSAL.capability_id, "0.1.0")
    assert draft_.description == "Member balance" and draft_.status is Status.DRAFT
    assert store.verification(draft_.id, "0.1.0")["kind"] == "success"  # type: ignore[index]
    review = client.get(f"/automations/{draft_.id}/0.1.0").text
    for n in range(1, len(draft_.steps) + 1):
        assert f'alt="after step {n}"' in review  # what each step did, from the checks
