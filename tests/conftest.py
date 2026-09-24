import socket
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from pathlib import Path

import httpx
import pytest
import uvicorn

from cua.control import InMemoryControl
from cua.evidence import RunLog
from cua.policy import Mode, Policy, PolicyConfig, Redactor
from cua.schema import AppModel, Capability, Status
from cua.secrets import EnvSecrets
from cua.session import GuardedSession
from cua.surface.web import WebSurface
from mock_bank.app import DEMO_PASSWORD, DEMO_USER, create_app

CATALOG = Path(__file__).parents[1] / "catalog"
FIXTURES = Path(__file__).parent / "fixtures"
SECRETS = EnvSecrets({"COREBANK_USER": DEMO_USER, "COREBANK_PASSWORD": DEMO_PASSWORD})


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def bank_servers() -> Iterator[dict[str, str]]:
    """One live mock-bank server per tenant variant, for the whole test session."""
    servers, urls = [], {}
    for variant in ("pinnacle", "riverbend"):
        port = _free_port()
        server = uvicorn.Server(
            uvicorn.Config(create_app(variant), host="127.0.0.1", port=port, log_level="warning")
        )
        threading.Thread(target=server.run, daemon=True).start()
        servers.append(server)
        urls[variant] = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 10
    while not all(s.started for s in servers):
        assert time.monotonic() < deadline, "mock bank servers did not start"
        time.sleep(0.05)
    yield urls
    for server in servers:
        server.should_exit = True


@pytest.fixture
def bank(bank_servers: dict[str, str]) -> Callable[[str], str]:
    """Base URL for a variant, with its data and faults reset."""

    def get(variant: str = "pinnacle") -> str:
        url = bank_servers[variant]
        httpx.post(f"{url}/__admin/reset").raise_for_status()
        return url

    return get


@pytest.fixture(scope="session")
def corebank() -> AppModel:
    return AppModel.model_validate_json((CATALOG / "corebank" / "app.json").read_text())


@pytest.fixture(scope="session")
def corebank_policy() -> PolicyConfig:
    return PolicyConfig.model_validate_json((CATALOG / "corebank" / "policy.json").read_text())


def load_capability(capability_id: str) -> Capability:
    """Hand-written test fixtures (not discovered artifacts; those live under catalog/)."""
    return Capability.model_validate_json((FIXTURES / f"{capability_id}.json").read_text())


@pytest.fixture
def browser() -> Iterator[WebSurface]:
    with WebSurface.launch() as surface:
        yield surface


OpenSession = Callable[..., GuardedSession]


@pytest.fixture
def open_session(
    tmp_path: Path, bank: Callable[[str], str], corebank: AppModel,
    corebank_policy: PolicyConfig, browser: WebSurface,
) -> Iterator[OpenSession]:
    """Open a GuardedSession on the test's browser against a freshly reset mock bank."""
    with ExitStack() as stack:

        def open_(*, variant: str = "pinnacle", mode: Mode = Mode.REPLAY,
                  status: Status | None = Status.APPROVED, control: InMemoryControl | None = None,
                  sign_on: bool = True, audit_targets: bool = False) -> GuardedSession:
            base = bank(variant)
            log = stack.enter_context(RunLog(tmp_path, Redactor()))
            session = stack.enter_context(GuardedSession.open(
                surface=browser, app=corebank, policy=Policy(corebank_policy, [base]),
                control=control or InMemoryControl(), log=log, secrets=SECRETS,
                env={"base_url": base}, mode=mode, capability_status=status,
                audit_targets=audit_targets,
            ))
            if sign_on:
                session.sign_on()
            return session

        yield open_
