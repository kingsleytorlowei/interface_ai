"""The operator console: a small web app over an `OperatorDesk`, run in-process on a background
thread. It shows pending approvals and handoffs (with the screenshot the session took), and
takes decisions: approve/reject, resume/abort, pause, take over.

It never drives the browser. During a handoff the person works directly in the headed
browser window: the live session itself.

Mock scope: bound to localhost, no authentication, operator identity is self-declared.
"""

import threading
import time
from pathlib import Path
from typing import Literal

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel

from cua.control import DeskError, OperatorDesk

STATIC = Path(__file__).parent / "static"
TEMPLATES = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"),
                        autoescape=select_autoescape())


class ApprovalDecision(BaseModel):
    approve: bool
    operator: str


class InterventionDecision(BaseModel):
    outcome: Literal["resumed", "aborted"]
    operator: str
    note: str = ""


class OperatorAction(BaseModel):
    operator: str


def create_console(desk: OperatorDesk, evidence_root: Path) -> FastAPI:
    """The console for one CLI run: just the approval and handoff cards."""
    app = FastAPI(title="Operator console", docs_url=None, redoc_url=None)
    add_desk_routes(app, desk, evidence_root)

    @app.get("/", response_class=HTMLResponse)
    def page() -> str:
        return TEMPLATES.get_template("console.html").render()

    return app


def add_desk_routes(app: FastAPI, desk: OperatorDesk, evidence_root: Path) -> None:
    """The desk's API (state, decisions, pause, take over), redacted evidence files, and the
    pages' shared styles and scripts."""
    evidence_root = evidence_root.resolve()
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    def decide(fn, *args) -> dict[str, str]:  # type: ignore[no-untyped-def]
        try:
            fn(*args)
        except DeskError as e:
            raise HTTPException(409, str(e)) from e
        return {"status": "ok"}

    @app.get("/api/state")
    def state() -> dict:  # type: ignore[type-arg]
        return desk.state()

    @app.post("/api/approvals/{item_id}")
    def approval(item_id: str, body: ApprovalDecision) -> dict[str, str]:
        return decide(desk.resolve_approval, item_id, body.approve, body.operator)

    @app.post("/api/interventions/{item_id}")
    def intervention(item_id: str, body: InterventionDecision) -> dict[str, str]:
        return decide(desk.resolve_intervention, item_id, body.outcome, body.operator,
                      body.note)

    @app.post("/api/pause")
    def pause(body: OperatorAction) -> dict[str, str]:
        if not body.operator.strip():
            raise HTTPException(409, "operator name is required")
        desk.pause()
        return {"status": "ok"}

    @app.post("/api/takeover")
    def takeover(body: OperatorAction) -> dict[str, str]:
        return decide(desk.take_over, body.operator)

    @app.get("/evidence/{ref:path}")
    def evidence(ref: str) -> FileResponse:
        path = (evidence_root / ref).resolve()
        if not path.is_relative_to(evidence_root) or path.suffix not in (".png", ".txt"):
            raise HTTPException(404)
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(path)


def serve_console(desk: OperatorDesk, evidence_root: Path, port: int = 8765) -> str:
    """Start the console on a daemon thread; returns its URL."""
    server = uvicorn.Server(uvicorn.Config(create_console(desk, evidence_root),
                                           host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("operator console did not start")
        time.sleep(0.05)
    return f"http://127.0.0.1:{port}"
