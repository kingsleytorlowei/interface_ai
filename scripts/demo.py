"""The whole demo in one command: both mock banks and the operator workbench, then the
workbench opens in your browser. Ctrl-C stops everything.

    uv run python scripts/demo.py              # add --headless to hide the teller window

Uses ports 8001 (Pinnacle), 8002 (Riverbend) and 8765 (workbench) when they're free, and
free ports otherwise. Discovering new automations needs ANTHROPIC_API_KEY in .env; the rest
doesn't.
"""

import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

import uvicorn

from cua.cli import CATALOG, EVIDENCE, load_dotenv
from cua.operator.workbench import WorkbenchConfig, create_workbench
from cua.store import Store
from mock_bank.app import DEMO_PASSWORD, DEMO_USER, create_app

ROOT = Path(__file__).resolve().parents[1]


def port(preferred: int) -> int:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", preferred))
        except OSError:
            s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def serve_in_background(app: object, at: int) -> uvicorn.Server:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=at,  # type: ignore[arg-type]
                                           log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError(f"nothing started on port {at}")
        time.sleep(0.05)
    return server


def main() -> int:
    os.chdir(ROOT)
    load_dotenv()
    os.environ.setdefault("COREBANK_USER", DEMO_USER)
    os.environ.setdefault("COREBANK_PASSWORD", DEMO_PASSWORD)
    banks = {}
    for variant, preferred in (("pinnacle", 8001), ("riverbend", 8002)):
        at = port(preferred)
        serve_in_background(create_app(variant), at)
        banks[variant] = f"http://127.0.0.1:{at}"
        print(f"mock bank ({variant}): {banks[variant]}")

    notes = CATALOG / "corebank" / "environment.md"
    config = WorkbenchConfig(Store(CATALOG), EVIDENCE, banks["pinnacle"],
                             {"riverbend": banks["riverbend"]},
                             headed="--headless" not in sys.argv[1:],
                             notes=notes.read_text() if notes.exists() else None)
    at = port(8765)
    url = f"http://127.0.0.1:{at}"
    print(f"operator workbench: {url}   (Ctrl-C to stop)")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("no ANTHROPIC_API_KEY: reviewing, approving and running work; discovering new "
              "automations doesn't")
    threading.Timer(1.5, webbrowser.open, args=[url]).start()
    uvicorn.run(create_workbench(config), host="127.0.0.1", port=at, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
