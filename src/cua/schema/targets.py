"""How a UI element is identified, independent of any surface technology.

Strategies are expressed in terms of what an operator perceives (role, accessible name, label,
nearby text, table position) so the same `Target` can be resolved from a browser accessibility
tree today and from UIA / AX trees on desktop. Selector strategies (`css`) are a last resort.

Resolution rule (implemented by surface adapters): try strategies in order; the first that
matches exactly one element wins; the winner is checked against the fingerprint. Multiple
matches or a fingerprint mismatch is `target_ambiguous` — never a silent pick. Winning with a
lower-ranked strategy is reported as a drift signal.
"""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .common import Model


class FrameSelector(Model):
    """One hop into a nested frame/iframe/frameset (legacy apps nest these deeply)."""

    name: str | None = None
    title: str | None = None
    url_contains: str | None = None

    @model_validator(mode="after")
    def _one_criterion(self) -> "FrameSelector":
        if not (self.name or self.title or self.url_contains):
            raise ValueError("frame selector needs name, title or url_contains")
        return self


class ByRole(Model):
    by: Literal["role"] = "role"
    role: str
    name: str | None = None
    exact: bool = True


class ByLabel(Model):
    by: Literal["label"] = "label"
    text: str


class ByText(Model):
    by: Literal["text"] = "text"
    text: str
    role: str | None = None


class ByNear(Model):
    """Element of `role` positioned in `direction` of visible `text` — works on table layouts
    where labels are not programmatically associated with inputs."""

    by: Literal["near"] = "near"
    text: str
    direction: Literal["right", "below", "left", "above"]
    role: str | None = None


class ByTableCell(Model):
    """Cell at the intersection of the row containing `row_has_text` and the column headed
    `column`."""

    by: Literal["table_cell"] = "table_cell"
    row_has_text: str
    column: str


class ByCss(Model):
    by: Literal["css"] = "css"
    selector: str


Strategy = Annotated[
    ByRole | ByLabel | ByText | ByNear | ByTableCell | ByCss, Field(discriminator="by")
]


class Fingerprint(Model):
    """What the element looked like at record time; verifies the resolved element and feeds
    drift detection."""

    role: str | None = None
    name: str | None = None
    tag: str | None = None


class Target(Model):
    frame: list[FrameSelector] = []
    strategies: list[Strategy] = Field(min_length=1)
    fingerprint: Fingerprint | None = None
