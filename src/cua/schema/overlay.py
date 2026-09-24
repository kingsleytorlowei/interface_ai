"""A tenant overlay: where one tenant's install puts the elements a base capability names.

Institutions running the same vendor product differ in labels, branding and layout, not in
flows. An overlay replaces named targets of one approved base capability for one tenant, and
nothing else: steps, risk, inputs, outputs, outcomes and states come from the base, so an
overlay can change where the flow acts but never what it does or what it promises the caller.
Reviewing one means reviewing locators, not a flow.

An overlay is pinned to an exact base version; a new base version doesn't inherit it.
"""

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from .capability import Capability, Status
from .common import DottedId, Model, SemVer, Slug
from .targets import Target


class BaseRef(Model):
    id: DottedId
    version: SemVer


class OverlayProvenance(Model):
    authored_by: str
    source: str  # the evidence that showed the drift, e.g. a failed replay on this tenant
    created_at: datetime
    reviewed_by: str | None = None


class CapabilityOverlay(Model):
    schema_version: Literal["1"] = "1"
    tenant: Slug
    base: BaseRef
    status: Status = Status.DRAFT
    # Whole targets, fingerprint included: a tenant's "Find" button is not a "Search" button
    # with an extra strategy, and mixing the two would blur what drift means.
    targets: dict[Slug, Target] = Field(min_length=1)
    provenance: OverlayProvenance

    @model_validator(mode="after")
    def _reviewed(self) -> "CapabilityOverlay":
        if self.status is Status.APPROVED and not self.provenance.reviewed_by:
            raise ValueError("approved overlays must record provenance.reviewed_by")
        return self


def apply_overlay(base: Capability, overlay: CapabilityOverlay) -> Capability:
    """The capability as it runs on the overlay's tenant. Its version carries the tenant as
    SemVer build metadata (`0.1.0+riverbend`), so results and evidence say what ran; it is
    approved only if both the base and the overlay are."""
    errors: list[str] = []
    if (overlay.base.id, overlay.base.version) != (base.id, base.version):
        errors.append(f"overlay is for {overlay.base.id}@{overlay.base.version}, "
                      f"not {base.id}@{base.version}")
    if unknown := overlay.targets.keys() - base.targets.keys():
        errors.append(f"overlay replaces targets the base doesn't have: {sorted(unknown)}")
    if errors:
        raise ValueError("; ".join(errors))
    approved = base.status is Status.APPROVED and overlay.status is Status.APPROVED
    return Capability.model_validate({
        **base.model_dump(),
        "version": f"{base.version}+{overlay.tenant}",
        "status": Status.APPROVED if approved else Status.DRAFT,
        "targets": {name: t.model_dump() for name, t in {**base.targets,
                                                          **overlay.targets}.items()},
    })
