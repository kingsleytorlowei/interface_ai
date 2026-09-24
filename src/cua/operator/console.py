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
from pydantic import BaseModel

from cua.control import DeskError, OperatorDesk


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
        return desk_page()

    return app


def desk_page(nav: str = "") -> str:
    """The cards page; the workbench passes its navigation bar in."""
    return PAGE.replace("<!--NAV-->", nav)


def add_desk_routes(app: FastAPI, desk: OperatorDesk, evidence_root: Path) -> None:
    """The desk's API (state, decisions, pause, take over) and redacted evidence files."""
    evidence_root = evidence_root.resolve()

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


PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Operator console</title>
<style>
  body { font: 14px system-ui, sans-serif; margin: 24px; max-width: 980px; color: #1a1a1a; }
  header { display: flex; gap: 12px; align-items: center; margin-bottom: 16px; }
  .card { border: 1px solid #ccc; border-radius: 6px; padding: 12px 14px; margin: 10px 0; }
  .pending { border-color: #c77700; background: #fffaf0; }
  .kind { font-weight: 600; text-transform: uppercase; font-size: 12px; letter-spacing: .04em; }
  .meta { color: #666; font-size: 12px; }
  img { max-width: 100%; border: 1px solid #ddd; margin-top: 8px; }
  button { margin-right: 6px; padding: 4px 10px; }
  .irreversible { color: #b00020; font-weight: 600; }
</style></head>
<body>
<!--NAV-->
<header>
  <strong>Operator console</strong>
  <label>Operator <input id="op" size="12" placeholder="your name"></label>
  <span id="holder" class="meta"></span>
  <button onclick="post('/api/pause', {})">Pause run</button>
  <button onclick="if (confirm('Take over ends the run.')) post('/api/takeover', {})">
    Take over</button>
</header>
<div id="items"></div>
<script>
const op = document.getElementById('op');
op.value = localStorage.getItem('operator') || '';
op.onchange = () => localStorage.setItem('operator', op.value);
const ENT = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ENT[c]);
async function post(url, body) {
  const r = await fetch(url, {method: 'POST', headers: {'content-type': 'application/json'},
                              body: JSON.stringify({...body, operator: op.value})});
  if (!r.ok) alert((await r.json()).detail);
  refresh();
}
function card(i) {
  const pending = i.status === 'pending';
  const shot = i.evidence_ref ? `<img src="/evidence/${esc(i.evidence_ref)}.png">` : '';
  const what = i.kind === 'approval'
    ? `<div><span class="${esc(i.risk)}">${esc(i.risk)}</span>: ${esc(i.action)}</div>` : '';
  const buttons = !pending ? '' : i.kind === 'approval'
    ? `<button onclick="post('/api/approvals/${esc(i.id)}', {approve: true})">Approve</button>
       <button onclick="post('/api/approvals/${esc(i.id)}', {approve: false})">Reject</button>`
    : `<div class="meta">Work in the automation's browser window, then:</div>
       <input id="note-${esc(i.id)}" size="50" placeholder="note (optional)">
       <button onclick="post('/api/interventions/${esc(i.id)}', {outcome: 'resumed',
         note: document.getElementById('note-${esc(i.id)}').value})">Hand back (resume)</button>
       <button onclick="post('/api/interventions/${esc(i.id)}', {outcome: 'aborted',
         note: document.getElementById('note-${esc(i.id)}').value})">End run (abort)</button>`;
  return `<div class="card ${pending ? 'pending' : ''}">
    <div class="kind">${esc(i.kind)} &middot; ${esc(i.status)}</div>
    <div>${esc(i.reason)}</div>${what}
    <div class="meta">run ${esc(i.run_id)} &middot; step ${esc(i.step_id || '-')} &middot;
      ${esc(i.created_at)}${i.operator ? ' &middot; by ' + esc(i.operator) : ''}</div>
    ${buttons}${pending ? shot : ''}</div>`;
}
async function refresh() {
  const s = await (await fetch('/api/state')).json();
  document.getElementById('holder').textContent =
    `control: ${s.holder || '-'}${s.pause_requested ? ' (pause requested)' : ''}`;
  const active = document.activeElement;
  if (active && active.id && active.id.startsWith('note-')) return;  // don't eat typing
  document.getElementById('items').innerHTML = s.items.map(card).join('') ||
    '<p class="meta">Nothing needs you right now.</p>';
}
refresh(); setInterval(refresh, 1000);
</script></body></html>
"""
