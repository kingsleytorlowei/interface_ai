"""Reproducibly record every run that needs no LLM and no human into evidence/recorded/.

    uv run python scripts/record_evidence.py

Starts its own mock banks (pinnacle + riverbend), replays each scenario through the same
GuardedSession stack the CLI uses, checks the result against what the scenario is meant to
show, and regenerates the recorded section of evidence/README.md. Exits 1 if any scenario
did not produce its expected result.

The lookup artifact is the approved, discovered one in catalog/ when it exists; otherwise the
hand-written test fixture, labelled as such. The sub-account flow is always the fixture (it
is never discovered: it is irreversible).
"""

import os
import shutil
import socket
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import uvicorn

from cua.control import InMemoryControl
from cua.evidence import RunLog
from cua.policy import Mode, Policy, Redactor
from cua.replay import replay
from cua.schema import Capability, RunResult, Status
from cua.secrets import EnvSecrets
from cua.session import GuardedSession
from cua.store import Store, StoreError
from cua.surface.web import WebSurface
from mock_bank.app import DEMO_PASSWORD, DEMO_USER, create_app

ROOT = Path(__file__).resolve().parents[1]
CATALOG = Path("catalog")
FIXTURES = Path("tests/fixtures")
RECORDED = Path("evidence/recorded")
INDEX = Path("evidence/README.md")
START, END = "<!-- recorded:start -->", "<!-- recorded:end -->"

LOOKUP = "corebank.member.lookup_balance"
OPEN_SUBACCOUNT = "corebank.subaccount.open"


@dataclass
class Source:
    capability: Capability
    label: str  # "discovered" or "fixture (hand-written)"


@dataclass
class Scenario:
    run_id: str
    title: str
    capability: str
    params: dict[str, Any]
    check: Callable[[RunResult], bool]
    expected: str
    variant: str = "pinnacle"
    faults: dict[str, int] = field(default_factory=dict)
    expire_sessions: bool = False


def recoveries(result: RunResult) -> list[str]:
    return [r.recovery for r in result.recoveries]


SCENARIOS = [
    Scenario("01-happy-path", "Happy path: known member", LOOKUP, {"member_id": "12345"},
             lambda r: r.kind == "success" and not r.recoveries,
             "success, typed outputs, no recoveries"),
    Scenario("02-business-outcome", "Business outcome: member not found", LOOKUP,
             {"member_id": "99999"},
             lambda r: r.kind == "business_outcome",
             "business_outcome (member not found), nothing committed"),
    # One fault per run: the mock's one-shot faults interact (a re-sign-on after an expired
    # session consumes a pending 503 before the replay ever sees it).
    Scenario("03a-recovery-session-expired", "Fault recovery: session expired mid-run",
             LOOKUP, {"member_id": "12345"},
             lambda r: r.kind == "success" and recoveries(r) == ["reauthenticate"],
             "success after re-authenticating", expire_sessions=True),
    Scenario("03b-recovery-transient-503", "Fault recovery: transient 503", LOOKUP,
             {"member_id": "12345"},
             lambda r: r.kind == "success" and recoveries(r) == ["retry"],
             "success after restarting from entry", faults={"transient_failures": 1}),
    Scenario("03c-recovery-broadcast-notice", "Fault recovery: broadcast notice interstitial",
             LOOKUP, {"member_id": "12345"},
             lambda r: r.kind == "success" and recoveries(r) == ["click"],
             "success after clicking through the notice", faults={"broadcast_notices": 1}),
    Scenario("04-fatal-failure", "Fatal failure: core system error on search", LOOKUP,
             {"member_id": "12345"},
             lambda r: r.kind == "failure" and r.category == "app_error",
             "failure/app_error with snapshot", faults={"fatal_errors": 1}),
    Scenario("05-riverbend-drift", "Tenant drift: pinnacle-recorded lookup on riverbend",
             LOOKUP, {"member_id": "12345"},
             lambda r: bool(r.drift) and r.kind in ("failure", "success"),
             "drift reported; explicit failure (or success on fallbacks), never a guess",
             variant="riverbend"),
    Scenario("06-approval-rejected", "Approval rejected: unattended irreversible step",
             OPEN_SUBACCOUNT,
             {"member_id": "12345", "account_type": "Holiday Club", "initial_deposit": "25.00"},
             lambda r: r.kind == "aborted" and r.committed_steps == [],
             "aborted at the approval gate, committed_steps == []"),
    # Slowness no screen can name: judged by the 10 s action budget, not the state library.
    Scenario("07a-recovery-hung-load", "Fault recovery: one page load hangs past the budget",
             LOOKUP, {"member_id": "12345"},
             lambda r: r.kind == "success" and recoveries(r) == ["retry"],
             "success after restarting from entry", faults={"stalled_loads": 1}),
    Scenario("07b-timeout", "Timeout: the app stays slower than the budget", LOOKUP,
             {"member_id": "12345"},
             lambda r: r.kind == "failure" and r.category == "timeout" and r.retryable,
             "failure/timeout after one retry, retryable (nothing committed)",
             faults={"latency_ms": 12000}),
]


def load_source(capability_id: str) -> Source:
    try:
        return Source(Store(CATALOG).load(capability_id, status=Status.APPROVED), "discovered")
    except StoreError:
        pass
    path = FIXTURES / f"{capability_id}.json"
    return Source(Capability.model_validate_json(path.read_text()), "fixture (hand-written)")


# --- mock banks -----------------------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def mock_banks() -> Iterator[dict[str, str]]:
    servers, urls = [], {}
    for variant in ("pinnacle", "riverbend"):
        port = free_port()
        server = uvicorn.Server(uvicorn.Config(
            create_app(variant), host="127.0.0.1", port=port, log_level="warning"))
        threading.Thread(target=server.run, daemon=True).start()
        servers.append(server)
        urls[variant] = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 10
    while not all(s.started for s in servers):
        if time.monotonic() > deadline:
            raise RuntimeError("mock banks did not start")
        time.sleep(0.05)
    try:
        yield urls
    finally:
        for server in servers:
            server.should_exit = True


# --- recording ------------------------------------------------------------------------------


def record(scenario: Scenario, source: Source, base: str, surface: WebSurface) -> RunResult:
    store = Store(CATALOG)
    httpx.post(f"{base}/__admin/reset").raise_for_status()
    with ExitStack() as stack:
        log = stack.enter_context(RunLog(RECORDED, Redactor(), run_id=scenario.run_id))
        session = stack.enter_context(GuardedSession.open(
            surface=surface, app=store.app("corebank"),
            policy=Policy(store.policy("corebank"), [base]),
            control=InMemoryControl(),  # unattended: rejects approvals, aborts handoffs
            log=log, secrets=EnvSecrets(), env={"base_url": base}, mode=Mode.REPLAY,
            capability_status=source.capability.status))
        # Faults are injected after sign-on so they hit the replay, not the log-in.
        session.sign_on()
        if scenario.faults:
            httpx.put(f"{base}/__admin/faults", json=scenario.faults).raise_for_status()
        if scenario.expire_sessions:
            httpx.post(f"{base}/__admin/expire-sessions").raise_for_status()
        result = replay(source.capability, scenario.params, session)
        log.write_json("capability.json", source.capability.model_dump(mode="json",
                                                                       exclude_none=True))
        log.write_json("scenario.json", {
            "scenario": scenario.title, "expected": scenario.expected,
            "artifact": f"{source.capability.id}@{source.capability.version}",
            "artifact_source": source.label, "tenant": scenario.variant,
            "faults": scenario.faults, "expire_sessions": scenario.expire_sessions,
            "input_names": sorted(scenario.params),  # values are sensitive: never written
        })
    return result


def summary(result: RunResult) -> str:
    detail = {"success": lambda: "",
              "business_outcome": lambda: result.code,
              "failure": lambda: f"{result.category} at `{result.step_id}`",
              "aborted": lambda: f"at `{result.step_id}`"}[result.kind]()
    parts = [f"`{result.kind}`" + (f" {detail}" if detail else "")]
    if result.recoveries:
        parts.append("recoveries: " + ", ".join(
            f"{r.state}→{r.recovery}" for r in result.recoveries))
    if result.drift:
        parts.append("drift: " + ", ".join(
            f"{d.target}[{d.strategy_index}]" for d in result.drift))
    parts.append(f"committed: {result.committed_steps or 'none'}")
    return "; ".join(parts)


def write_index(rows: list[tuple[Scenario, Source, RunResult, bool]]) -> None:
    lines = [
        START,
        "_Generated by `uv run python scripts/record_evidence.py`; do not edit by hand._",
        "",
        "| Scenario | Artifact | Result | Run |",
        "|---|---|---|---|",
    ]
    for scenario, source, result, ok in rows:
        artifact = f"`{source.capability.id}@{source.capability.version}` ({source.label})"
        mark = "" if ok else " **UNEXPECTED**"
        lines.append(f"| {scenario.title} | {artifact} | {summary(result)}{mark} | "
                     f"[{scenario.run_id}](recorded/{scenario.run_id}/) |")
    lines.append(END)
    text = INDEX.read_text()
    head, _, rest = text.partition(START)
    _, _, tail = rest.partition(END)
    INDEX.write_text(head + "\n".join(lines) + tail)


def main() -> int:
    os.chdir(ROOT)
    os.environ.setdefault("COREBANK_USER", DEMO_USER)
    os.environ.setdefault("COREBANK_PASSWORD", DEMO_PASSWORD)
    shutil.rmtree(RECORDED, ignore_errors=True)
    RECORDED.mkdir(parents=True)

    rows = []
    with mock_banks() as urls:
        for scenario in SCENARIOS:
            source = load_source(scenario.capability)
            # A fresh browser per run, as the CLI does: no state leaks between scenarios.
            with WebSurface.launch() as surface:
                result = record(scenario, source, urls[scenario.variant], surface)
            ok = scenario.check(result)
            rows.append((scenario, source, result, ok))
            print(f"{'ok ' if ok else 'BAD'} {scenario.run_id:22} {summary(result)}")
    write_index(rows)
    return 0 if all(ok for *_, ok in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
