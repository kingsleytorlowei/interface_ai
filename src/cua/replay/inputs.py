"""Validate and coerce invocation parameters against a capability's `InputSpec`s, before the
application is touched. Strict on purpose: a typed API rejects what it can't represent
exactly (a float for money, a third decimal place) rather than guessing.
"""

import re
from collections.abc import Mapping
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from cua.schema import InputSpec, ParamType

InputValue = str | int | Decimal | bool | date

_DECIMAL_RE = re.compile(r"^-?\d+(\.\d+)?$")


class InputError(ValueError):
    def __init__(self, problems: Mapping[str, str]) -> None:
        super().__init__("; ".join(f"{k}: {v}" for k, v in problems.items()))
        self.problems = dict(problems)


def coerce_inputs(
    specs: Mapping[str, InputSpec], params: Mapping[str, Any]
) -> dict[str, InputValue]:
    problems: dict[str, str] = {}
    values: dict[str, InputValue] = {}
    for name in params.keys() - specs.keys():
        problems[name] = "unknown input"
    for name, spec in specs.items():
        if name not in params or params[name] is None:
            problems[name] = "required"
            continue
        try:
            values[name] = _coerce(spec, params[name])
        except ValueError as e:
            problems[name] = str(e)
    if problems:
        raise InputError(dict(sorted(problems.items())))
    return values


def _coerce(spec: InputSpec, raw: Any) -> InputValue:
    match spec.type:
        case ParamType.STRING:
            if not isinstance(raw, str):
                raise ValueError(f"expected a string, got {type(raw).__name__}")
            if spec.pattern and not re.fullmatch(spec.pattern, raw):
                raise ValueError(f"does not match {spec.pattern}")
            if spec.enum and raw not in spec.enum:
                raise ValueError(f"must be one of {spec.enum}")
            return raw
        case ParamType.INTEGER:
            if isinstance(raw, bool) or not isinstance(raw, int | str):
                raise ValueError(f"expected an integer, got {type(raw).__name__}")
            if isinstance(raw, str) and not re.fullmatch(r"-?\d+", raw):
                raise ValueError(f"{raw!r} is not an integer")
            return int(raw)
        case ParamType.DECIMAL | ParamType.MONEY:
            if isinstance(raw, bool) or not isinstance(raw, int | str | Decimal):
                raise ValueError(f"expected a decimal string, got {type(raw).__name__}"
                                 " (floats are not exact)")
            text = str(raw)
            if not _DECIMAL_RE.fullmatch(text):
                raise ValueError(f"{text!r} is not a plain decimal number")
            try:
                value = Decimal(text)
            except InvalidOperation as e:
                raise ValueError(f"{text!r} is not a number") from e
            if spec.type is ParamType.MONEY:
                if value.as_tuple().exponent < -2:  # type: ignore[operator]
                    raise ValueError("money has at most 2 decimal places")
                value = value.quantize(Decimal("0.01"))
            return value
        case ParamType.BOOLEAN:
            if isinstance(raw, bool):
                return raw
            if raw in ("true", "false"):
                return str(raw) == "true"
            raise ValueError("expected a boolean")
        case ParamType.DATE:
            if isinstance(raw, date):
                return raw
            if isinstance(raw, str):
                try:
                    return date.fromisoformat(raw)
                except ValueError as e:
                    raise ValueError(f"{raw!r} is not an ISO date (YYYY-MM-DD)") from e
            raise ValueError("expected an ISO date")
    raise AssertionError(f"unhandled type {spec.type}")


def template_value(value: InputValue) -> str:
    """How a typed value is typed into the application."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)  # Decimal keeps its places ("25.00"); date is ISO
