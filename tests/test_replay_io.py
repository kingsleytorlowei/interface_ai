from datetime import date
from decimal import Decimal

import pytest

from cua.replay import InputError, OutputError, coerce_inputs, parse_output
from cua.replay.inputs import template_value
from cua.schema import InputSpec, OutputSpec, ParamType

S = ParamType


def spec(t: ParamType, **kw) -> InputSpec:  # type: ignore[no-untyped-def]
    return InputSpec(type=t, description="x", **kw)


def test_inputs_are_coerced_to_their_types() -> None:
    specs = {
        "member_id": spec(S.STRING, pattern=r"^\d{5}$"),
        "kind": spec(S.STRING, enum=["a", "b"]),
        "n": spec(S.INTEGER),
        "amount": spec(S.MONEY),
        "rate": spec(S.DECIMAL),
        "flag": spec(S.BOOLEAN),
        "on": spec(S.DATE),
    }
    values = coerce_inputs(specs, {
        "member_id": "12345", "kind": "b", "n": "42", "amount": 25, "rate": "0.125",
        "flag": "true", "on": "2026-09-23",
    })
    assert values == {"member_id": "12345", "kind": "b", "n": 42, "amount": Decimal("25.00"),
                      "rate": Decimal("0.125"), "flag": True, "on": date(2026, 9, 23)}
    assert template_value(values["amount"]) == "25.00"
    assert template_value(values["flag"]) == "true"


def test_input_problems_are_all_reported_at_once() -> None:
    specs = {"member_id": spec(S.STRING, pattern=r"^\d{5}$"), "amount": spec(S.MONEY),
             "n": spec(S.INTEGER), "flag": spec(S.BOOLEAN)}
    with pytest.raises(InputError) as e:
        coerce_inputs(specs, {"member_id": "12a", "amount": 1.5, "n": True, "extra": 1})
    assert set(e.value.problems) == {"member_id", "amount", "n", "flag", "extra"}
    assert "floats are not exact" in e.value.problems["amount"]
    assert e.value.problems["flag"] == "required"
    assert e.value.problems["extra"] == "unknown input"


@pytest.mark.parametrize("raw", ["25.001", "$25", "1,000.00", "1e3"])
def test_money_inputs_are_strict(raw: str) -> None:
    with pytest.raises(InputError):
        coerce_inputs({"a": spec(S.MONEY)}, {"a": raw})


@pytest.mark.parametrize(
    ("text", "type_", "value"),
    [
        ("$4,210.37", S.MONEY, Decimal("4210.37")),
        ("($12.50)", S.MONEY, Decimal("-12.50")),
        ("-$5", S.MONEY, Decimal("-5.00")),
        ("1,022", S.INTEGER, 1022),
        ("0.125", S.DECIMAL, Decimal("0.125")),
        ("  Jane   Q. Sample ", S.STRING, "Jane Q. Sample"),
        ("Yes", S.BOOLEAN, True),
        ("09/23/2026", S.DATE, date(2026, 9, 23)),
        ("23-Sep-2026", S.DATE, date(2026, 9, 23)),
    ],
)
def test_outputs_parse_display_formats(text: str, type_: ParamType, value: object) -> None:
    assert parse_output(text, OutputSpec(type=type_, description="x")) == value


@pytest.mark.parametrize(
    ("text", "type_"),
    [("", S.STRING), ("N/A", S.MONEY), ("$4,21.00", S.MONEY), ("(5.00", S.MONEY),
     ("1.5", S.INTEGER), ("$1.005", S.MONEY), ("maybe", S.BOOLEAN), ("9/23", S.DATE)],
)
def test_outputs_never_guess(text: str, type_: ParamType) -> None:
    with pytest.raises(OutputError):
        parse_output(text, OutputSpec(type=type_, description="x"))
