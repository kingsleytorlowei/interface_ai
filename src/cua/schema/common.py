"""Shared primitives for the artifact schema."""

import re
from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints


class Model(BaseModel):
    """Base for all artifact types: immutable, and unknown fields are an error (not ignored),
    so a typo in a reviewed artifact can't silently change behaviour."""

    model_config = ConfigDict(extra="forbid", frozen=True)


Slug = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$")]
DottedId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")]
SemVer = Annotated[str, StringConstraints(pattern=r"^\d+\.\d+\.\d+$")]
# A capability as it runs on one tenant carries the tenant as SemVer build metadata
# (`0.1.0+riverbend`); only plain versions are ever stored.
BuildVersion = Annotated[
    str, StringConstraints(pattern=r"^\d+\.\d+\.\d+(\+[a-z][a-z0-9_]*)?$")]

# `{{inputs.member_id}}` (per-invocation parameter) or `{{env.base_url}}` (per-tenant runtime).
TEMPLATE_RE = re.compile(r"\{\{\s*(inputs|env)\.([a-z][a-z0-9_]*)\s*\}\}")


def template_refs(text: str, namespace: str) -> set[str]:
    return {name for ns, name in TEMPLATE_RE.findall(text) if ns == namespace}


def render_template(
    text: str, *, inputs: Mapping[str, object] | None = None, env: Mapping[str, str] | None = None
) -> str:
    sources: dict[str, Mapping[str, object]] = {"inputs": inputs or {}, "env": env or {}}

    def substitute(m: re.Match[str]) -> str:
        ns, name = m.groups()
        if name not in sources[ns]:
            raise KeyError(f"template references {ns}.{name}, which was not provided")
        return str(sources[ns][name])

    return TEMPLATE_RE.sub(substitute, text)


class Sensitivity(StrEnum):
    """Drives redaction everywhere. `secret` is never a legal input or output: credentials are
    resolved by the runtime from a secret reference and never enter the artifact or logs."""

    PUBLIC = "public"
    INTERNAL = "internal"
    PII = "pii"
    CONFIDENTIAL = "confidential"
    SECRET = "secret"

    @property
    def masked_in_logs(self) -> bool:
        return self in (Sensitivity.PII, Sensitivity.CONFIDENTIAL, Sensitivity.SECRET)


class Risk(StrEnum):
    READ_ONLY = "read_only"
    REVERSIBLE = "reversible"
    IRREVERSIBLE = "irreversible"

    @property
    def rank(self) -> int:
        return list(Risk).index(self)
