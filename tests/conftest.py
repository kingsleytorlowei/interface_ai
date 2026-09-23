import socket
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
import uvicorn

from cua.schema import AppModel
from mock_bank.app import create_app

CATALOG = Path(__file__).parents[1] / "catalog"


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
