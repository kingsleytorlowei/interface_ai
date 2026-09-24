"""Artifact persistence under `catalog/`: load/save, semver, approval state (draft -> approved).

    catalog/<app>/app.json                                  state library
    catalog/<app>/policy.json                               guardrails
    catalog/<app>/capabilities/<id>@<version>.json          capability artifact
    catalog/<app>/capabilities/<id>@<version>.verification.json   verification replay result

A capability is only loadable if it fits its app's state library, and only approvable once a
verification replay has succeeded. Approved artifacts are immutable: changes mean a new
version.
"""

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from cua.policy import PolicyConfig, Redactor
from cua.schema import AppModel, Capability, Risk, Status, check_against_app


class StoreError(Exception):
    pass


class Store:
    def __init__(self, root: Path) -> None:
        self.root = root

    def app(self, app_id: str) -> AppModel:
        return AppModel.model_validate_json((self.root / app_id / "app.json").read_text())

    def policy(self, app_id: str) -> PolicyConfig:
        return PolicyConfig.model_validate_json((self.root / app_id / "policy.json").read_text())

    def _path(self, capability_id: str, version: str, suffix: str = ".json") -> Path:
        app_id = capability_id.split(".")[0]
        return self.root / app_id / "capabilities" / f"{capability_id}@{version}{suffix}"

    def versions(self, capability_id: str) -> list[str]:
        folder = self.root / capability_id.split(".")[0] / "capabilities"
        found = [p.name[len(capability_id) + 1:-len(".json")]
                 for p in folder.glob(f"{capability_id}@*.json")
                 if not p.name.endswith(".verification.json")]
        return sorted(found, key=lambda v: tuple(int(x) for x in v.split(".")))

    def next_draft_version(self, capability_id: str) -> str:
        """Where a new draft goes: 0.1.0 at first, then the next patch after an approved
        latest version (approved is immutable). An unapproved latest draft is replaced."""
        versions = self.versions(capability_id)
        if not versions:
            return "0.1.0"
        latest = versions[-1]
        if self._status(capability_id, latest) != Status.APPROVED:
            return latest
        major, minor, patch = (int(x) for x in latest.split("."))
        return f"{major}.{minor}.{patch + 1}"

    def _status(self, capability_id: str, version: str) -> str | None:
        return json.loads(self._path(capability_id, version).read_text()).get("status")

    def load(self, capability_id: str, version: str | None = None, *,
             status: Status | None = None) -> Capability:
        """A given version, or else the latest one (with `status`, the latest in that status:
        callers invoking a capability want the latest approved one, not a newer draft)."""
        versions = self.versions(capability_id)
        if not versions:
            raise StoreError(f"no capability {capability_id!r}")
        if version is None:
            candidates = [v for v in versions
                          if status is None or self._status(capability_id, v) == status]
            if not candidates:
                raise StoreError(f"no {status} version of {capability_id} (have {versions}); "
                                 "approve one or pass --version")
            version = candidates[-1]
        path = self._path(capability_id, version)
        if not path.exists():
            raise StoreError(f"no version {version} of {capability_id} (have {versions})")
        capability = Capability.model_validate_json(path.read_text())
        if errors := check_against_app(capability, self.app(capability.app.app_id)):
            raise StoreError(f"{capability_id}@{version} does not fit its app: {errors}")
        return capability

    def save(self, capability: Capability, verification: dict[str, Any] | None = None, *,
             sensitive: Iterable[Redactor] = ()) -> Path:
        """Write a capability (and its verification record). `sensitive`: redactors holding
        the values a run knew to be sensitive; an artifact containing any of them is refused
        (artifacts are shared across tenants and must never carry data)."""
        text = capability.model_dump_json(indent=2, exclude_none=True) + "\n"
        if leaked := sorted({label for r in sensitive for label in r.labels_in(text)}):
            raise StoreError(f"{capability.id}@{capability.version} contains sensitive "
                             f"values ({', '.join(leaked)}); not saved")
        path = self._path(capability.id, capability.version)
        if path.exists():
            existing = Capability.model_validate_json(path.read_text())
            if existing.status is Status.APPROVED:
                raise StoreError(f"{capability.id}@{capability.version} is approved and "
                                 "immutable; bump the version")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        if verification is not None:
            self._path(capability.id, capability.version, ".verification.json").write_text(
                json.dumps(verification, indent=2, default=str) + "\n")
        return path

    def verification(self, capability_id: str, version: str) -> dict[str, Any] | None:
        path = self._path(capability_id, version, ".verification.json")
        return json.loads(path.read_text()) if path.exists() else None

    def approve(self, capability_id: str, version: str, reviewer: str, *,
                read_only_steps: list[str] | None = None) -> Capability:
        """A human signs off. They may lower query-only steps to read_only (the policy still
        enforces its own floor at run time); nothing else is editable at approval."""
        capability = self.load(capability_id, version)
        if capability.status is not Status.DRAFT:
            raise StoreError(f"{capability_id}@{version} is {capability.status}, not draft")
        verification = self.verification(capability_id, version)
        if not verification or verification.get("kind") != "success":
            raise StoreError("approval needs a successful verification replay first "
                             f"(have: {verification and verification.get('kind')})")
        lowered = set(read_only_steps or [])
        if unknown := lowered - {s.id for s in capability.steps}:
            raise StoreError(f"unknown steps {sorted(unknown)}")
        if not_reversible := [s.id for s in capability.steps
                              if s.id in lowered and s.risk is not Risk.REVERSIBLE]:
            raise StoreError(f"only reversible steps can be lowered: {not_reversible}")
        steps = [s.model_copy(update={"risk": Risk.READ_ONLY}) if s.id in lowered else s
                 for s in capability.steps]
        approved = Capability.model_validate({
            **capability.model_dump(),
            "steps": [s.model_dump() for s in steps],
            "risk": max((s.risk for s in steps), key=lambda r: r.rank),
            "status": Status.APPROVED,
            "provenance": {**capability.provenance.model_dump(), "reviewed_by": reviewer},
        })
        self.save(approved)
        return approved
