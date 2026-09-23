import json
from pathlib import Path

import pytest

from cua.control import ApprovalRequest, InMemoryControl, InterventionRequest
from cua.evidence import RunLog
from cua.policy import Redactor
from cua.schema import Fill, Observation, Risk, render_template
from cua.secrets import EnvSecrets


def events(log: RunLog) -> list[dict]:  # type: ignore[type-arg]
    return [json.loads(line) for line in (log.dir / "events.jsonl").read_text().splitlines()]


def test_every_sink_is_redacted(tmp_path: Path) -> None:
    redactor = Redactor()
    redactor.register("12345", "inputs.member_id")
    with RunLog(tmp_path, redactor) as log:
        log.emit("action_started", "search", action=Fill(target="member_id", value="12345"),
                 risk=Risk.READ_ONLY, error="no member 12345")
        obs = Observation(url="http://x/inquiry?m=12345", title="t", frame_urls=[],
                          text="Member No: 12345", tree=[], snapshot='- cell "12345"')
        path = log.snapshot("failed", observation=obs, screenshot=b"png")
        log.write_json("result.json", {"outputs": {"member": 12345}})

    everything = "".join(p.read_text(errors="ignore") for p in log.dir.rglob("*") if p.is_file()
                         and p.suffix != ".png")
    assert "12345" not in everything
    assert "«inputs.member_id»" in everything

    first, snap = events(log)
    assert [first["seq"], snap["seq"]] == [1, 2]
    assert first["data"]["action"] == {"kind": "fill", "target": "member_id",
                                       "value": "«inputs.member_id»"}
    assert first["data"]["risk"] == "read_only"
    assert (log.dir / f"{path}.png").read_bytes() == b"png"
    assert snap["data"] == {"label": "failed", "path": path, "screenshot": True}


def test_run_dirs_are_never_reused(tmp_path: Path) -> None:
    RunLog(tmp_path, Redactor(), run_id="r1").close()
    with pytest.raises(FileExistsError):
        RunLog(tmp_path, Redactor(), run_id="r1")


def test_lease_epochs_invalidate_earlier_holders() -> None:
    control = InMemoryControl()
    agent = control.acquire("agent:r1")
    assert control.is_current(agent)
    operator = control.acquire("operator:alice")
    assert not control.is_current(agent) and control.is_current(operator)
    assert control.acquire("agent:r1") != agent  # re-acquiring is a new epoch, not the old one


def test_unattended_control_fails_closed() -> None:
    control = InMemoryControl()
    approval = ApprovalRequest("r1", "confirm", 'click button "Confirm"', Risk.IRREVERSIBLE, "x")
    assert control.request_approval(approval, timeout_s=1) is False
    resolution = control.request_intervention(InterventionRequest("iv1", "r1", None, "alert"), 1)
    assert resolution.outcome == "aborted"
    assert control.approvals == [approval]


def test_env_secrets_never_echo_values() -> None:
    secrets = EnvSecrets({"COREBANK_PASSWORD": "s3cret", "EMPTY": ""})
    assert secrets.get("env:COREBANK_PASSWORD") == "s3cret"
    for ref in ("env:MISSING", "env:EMPTY", "vault:x", "COREBANK_PASSWORD"):
        with pytest.raises(KeyError):
            secrets.get(ref)


def test_render_template() -> None:
    assert render_template("{{env.base_url}}/m/{{ inputs.id }}",
                           inputs={"id": 7}, env={"base_url": "http://x"}) == "http://x/m/7"
    with pytest.raises(KeyError, match="inputs.missing"):
        render_template("{{inputs.missing}}")
