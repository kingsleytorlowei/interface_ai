import copy
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from cua.schema import (
    AppModel,
    Capability,
    RunResult,
    Success,
    Target,
    check_against_app,
    describe,
)

APP: dict[str, Any] = {
    "app_id": "corebank",
    "version": "1.0.0",
    "states": {
        "member_search": {"match": [{"kind": "element", "role": "textbox", "name": "Member ID"}]},
        "member_detail": {"match": [{"kind": "text", "text": "Member Summary"}]},
        "no_results": {"match": [{"kind": "text_regex", "pattern": "No (member|records) found"}]},
        "confirm_dialog": {
            "match": [{"kind": "element", "role": "dialog", "name": "Notice"}],
            "kind": "interstitial",
            "recovery": {
                "kind": "click",
                "target": {"strategies": [{"by": "role", "role": "button", "name": "OK"}]},
            },
        },
        "app_error": {
            "match": [{"kind": "text_regex", "pattern": "System error"}], "kind": "fatal",
        },
    },
}

CONTENT_FRAME = [{"name": "content"}]

CAPABILITY: dict[str, Any] = {
    "id": "corebank.lookup_savings_balance",
    "version": "1.0.0",
    "description": "Look up a member by ID and read their current savings balance.",
    "app": {"app_id": "corebank", "compat": ">=7.2 <8"},
    "risk": "read_only",
    "entry": {"url": "{{env.base_url}}/main.html"},
    "inputs": {
        "member_id": {"type": "string", "pattern": r"^\d{5}$", "description": "5-digit member no."}
    },
    "outputs": {
        "member_name": {"type": "string", "sensitivity": "pii", "description": "Full name"},
        "savings_balance": {
            "type": "money", "sensitivity": "confidential", "description": "Current balance",
        },
    },
    "outcomes": {
        "member_not_found": {"description": "No member with this ID", "when_state": "no_results"}
    },
    "targets": {
        "member_id_field": {
            "frame": CONTENT_FRAME,
            "strategies": [
                {"by": "role", "role": "textbox", "name": "Member ID"},
                {"by": "near", "text": "Member ID", "direction": "right", "role": "textbox"},
                {"by": "css", "selector": "input[name=mbrno]"},
            ],
            "fingerprint": {"role": "textbox", "name": "Member ID", "tag": "input"},
        },
        "search_button": {
            "frame": CONTENT_FRAME,
            "strategies": [{"by": "role", "role": "button", "name": "Search"}],
        },
        "member_name_cell": {
            "frame": CONTENT_FRAME,
            "strategies": [{"by": "near", "text": "Name:", "direction": "right", "role": "cell"}],
        },
        "savings_balance_cell": {
            "frame": CONTENT_FRAME,
            "strategies": [{"by": "table_cell", "row_has_text": "Savings", "column": "Balance"}],
        },
    },
    "steps": [
        {
            "id": "enter_member_id",
            "intent": "Enter the member ID in the search field",
            "action": {"kind": "fill", "target": "member_id_field",
                       "value": "{{inputs.member_id}}"},
            "risk": "read_only",
        },
        {
            "id": "submit_search",
            "intent": "Submit the search",
            "action": {"kind": "click", "target": "search_button"},
            "risk": "read_only",
            "expect": {"state": "member_detail"},
        },
        {
            "id": "read_name",
            "intent": "Read the member's name",
            "action": {"kind": "extract", "target": "member_name_cell", "into": "member_name"},
            "risk": "read_only",
        },
        {
            "id": "read_balance",
            "intent": "Read the savings balance",
            "action": {"kind": "extract", "target": "savings_balance_cell",
                       "into": "savings_balance"},
            "risk": "read_only",
        },
    ],
    "success": {"state": "member_detail", "outputs_present": ["member_name", "savings_balance"]},
    "provenance": {"run_id": "disc_test", "model": "test", "recorded_at": "2026-09-23T00:00:00Z"},
}


def cap(**overrides: Any) -> dict[str, Any]:
    data = copy.deepcopy(CAPABILITY)
    data.update(overrides)
    return data


def test_example_capability_is_valid_and_consistent_with_app() -> None:
    capability = Capability.model_validate(CAPABILITY)
    assert check_against_app(capability, AppModel.model_validate(APP)) == []


def test_json_round_trip_is_lossless() -> None:
    capability = Capability.model_validate(CAPABILITY)
    assert Capability.model_validate_json(capability.model_dump_json()) == capability


def test_json_schema_exports() -> None:
    schema = Capability.model_json_schema()
    assert {"inputs", "outputs", "outcomes", "steps", "targets"} <= schema["properties"].keys()
    json.dumps(schema)


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        Capability.model_validate(cap(retries=3))


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda c: c["steps"].append(copy.deepcopy(c["steps"][0])), "duplicate step ids"),
        (lambda c: c["steps"][1]["action"].update(target="nope"), "undefined targets"),
        (lambda c: c["targets"].update(extra=c["targets"]["search_button"]), "never used"),
        (lambda c: c["steps"][0]["action"].update(value="{{inputs.other}}"), "undeclared inputs"),
        (lambda c: c["inputs"].update(unused={"type": "string", "description": "x"}),
         "inputs declared but never used"),
        (lambda c: c["steps"].pop(), "outputs never extracted"),
        (lambda c: c.update(risk="irreversible"), "must equal highest step risk"),
        (lambda c: c.update(id="otherapp.lookup"), "namespaced by app_id"),
        (lambda c: c.update(status="approved"), "reviewed_by"),
        (lambda c: c["outcomes"].update(ok={"description": "x", "when_state": "member_detail"}),
         "on the success path"),
        (lambda c: c["outcomes"].update(gone={"description": "x", "when_state": "no_results"}),
         "same screen"),
        (lambda c: c["inputs"]["member_id"].update(sensitivity="secret"), "secrets are never"),
        (lambda c: c["inputs"]["member_id"].update(type="integer"), "pattern/enum only"),
        (lambda c: c["targets"]["search_button"].update(strategies=[]), "at least 1"),
    ],
)
def test_invalid_capabilities_are_rejected(mutate: Any, message: str) -> None:
    data = cap()
    mutate(data)
    with pytest.raises(ValidationError, match=message):
        Capability.model_validate(data)


def test_state_references_are_checked_against_app() -> None:
    data = cap()
    data["steps"][1]["expect"] = {"state": "nonexistent"}
    data["outcomes"]["member_not_found"]["when_state"] = "confirm_dialog"
    errors = check_against_app(Capability.model_validate(data), AppModel.model_validate(APP))
    assert any("unknown states: ['nonexistent']" in e for e in errors)
    assert any("must map to a screen state" in e for e in errors)


def test_interstitial_requires_recovery() -> None:
    app = copy.deepcopy(APP)
    del app["states"]["confirm_dialog"]["recovery"]
    with pytest.raises(ValidationError, match="interstitial states need a recovery"):
        AppModel.model_validate(app)


def test_run_result_is_a_discriminated_union() -> None:
    now = datetime.now(UTC)
    result = Success(
        run_id="r1", capability_id=CAPABILITY["id"], capability_version="1.0.0",
        started_at=now, finished_at=now, evidence_ref="runs/r1",
        outputs={"savings_balance": Decimal("1234.56")},
    )
    parsed = TypeAdapter(RunResult).validate_json(result.model_dump_json())
    assert isinstance(parsed, Success)
    assert parsed.outputs["savings_balance"] == "1234.56"  # money serialises as exact string


def test_catalog_app_model_is_valid() -> None:
    from pathlib import Path

    path = Path(__file__).parents[1] / "catalog" / "corebank" / "app.json"
    app = AppModel.model_validate_json(path.read_text())
    assert app.states["member_alert"].recovery.kind == "escalate"  # type: ignore[union-attr]


# --- targets in words -----------------------------------------------------------------------


@pytest.mark.parametrize("strategy,words", [
    ({"by": "role", "role": "button", "name": "Search"}, 'button "Search"'),
    ({"by": "label", "text": "Member ID"}, 'field labelled "Member ID"'),
    ({"by": "text", "text": "Continue", "role": "link"}, 'link showing "Continue"'),
    ({"by": "near", "text": "Member ID", "direction": "right", "role": "textbox"},
     'text box right of "Member ID"'),
    ({"by": "table_cell", "row_has_text": "Share Savings", "column": "Balance"},
     'cell in row "Share Savings", column "Balance"'),
    ({"by": "css", "selector": "input[name=mbrno]"}, "a technical fallback (page markup)"),
])
def test_targets_are_described_in_words(strategy: dict[str, str], words: str) -> None:
    target = Target.model_validate({"strategies": [strategy, {"by": "css", "selector": "x"}]})
    assert describe(target) == words  # the preferred strategy, never the fallback
