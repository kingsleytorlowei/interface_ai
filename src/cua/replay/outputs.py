"""Parse text read from the screen into an output's declared type. Tolerant of display
formatting (currency symbols, thousands separators, accounting negatives) but never of
ambiguity: anything that isn't clearly the declared type is an error, not a best guess.
"""

import re
from datetime import datetime
from decimal import Decimal

from cua.schema import OutputSpec, ParamType
from cua.schema.results import OutputValue

_NUMBER_RE = re.compile(
    r"^(?P<neg>-|\()?\s*\$?\s*(?P<digits>\d{1,3}(,\d{3})*|\d+)(?P<frac>\.\d+)?\s*\)?$"
)
_DATE_FORMATS = ("%m/%d/%Y", "%Y-%m-%d", "%d-%b-%Y")
_TRUE, _FALSE = {"yes", "y", "true"}, {"no", "n", "false"}


class OutputError(ValueError):
    pass


def parse_output(text: str, spec: OutputSpec) -> OutputValue:
    text = " ".join(text.split())
    if not text:
        raise OutputError("empty")
    match spec.type:
        case ParamType.STRING:
            return text
        case ParamType.INTEGER | ParamType.DECIMAL | ParamType.MONEY:
            m = _NUMBER_RE.fullmatch(text)
            if m is None or (m["neg"] == "(") != text.endswith(")"):
                raise OutputError(f"{text!r} is not a number")
            if spec.type is ParamType.INTEGER and m["frac"]:
                raise OutputError(f"{text!r} is not an integer")
            value = Decimal(m["digits"].replace(",", "") + (m["frac"] or ""))
            value = -value if m["neg"] else value
            if spec.type is ParamType.INTEGER:
                return int(value)
            if spec.type is ParamType.MONEY:
                if value.as_tuple().exponent < -2:  # type: ignore[operator]
                    raise OutputError(f"{text!r} has more than 2 decimal places")
                return value.quantize(Decimal("0.01"))
            return value
        case ParamType.BOOLEAN:
            if text.lower() in _TRUE | _FALSE:
                return text.lower() in _TRUE
            raise OutputError(f"{text!r} is not a yes/no value")
        case ParamType.DATE:
            for fmt in _DATE_FORMATS:
                try:
                    return datetime.strptime(text, fmt).date()
                except ValueError:
                    continue
            raise OutputError(f"{text!r} is not a recognised date")
    raise AssertionError(f"unhandled type {spec.type}")
