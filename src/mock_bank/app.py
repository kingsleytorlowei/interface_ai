"""CoreOne Teller — a deliberately legacy mock core-banking app (the proxy target).

Legacy traits on purpose: framesets, table layouts, labels not associated with inputs, cryptic
field names, no ids or test ids, full-page form posts, and a URL that doesn't identify the
screen (search results render at POST /inquiry).

Runtime conditions come from two places:
- data (always on): not-found (99999), validation errors, member alert interstitial (23456),
  permission denial (34567), minimum-deposit validation on sub-accounts;
- injected faults, toggled via /__admin (not linked from the UI, and outside the agent's
  allowlist): latency, transient 503s, fatal system errors, broadcast notices, session expiry.
"""

import asyncio
import secrets
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from .data import SUB_ACCOUNT_TYPES, Account, Store
from .variants import VARIANTS

SESSION_COOKIE = "JSESSIONID"
DEMO_USER = "teller1"
DEMO_PASSWORD = "demo-only-password"  # fake app, fake credential; the engine reads it from env


class Faults(BaseModel):
    latency_ms: int = 0
    transient_failures: int = 0  # next N content page loads return 503
    fatal_errors: int = 0  # next N member searches return a system error page
    broadcast_notices: int = 0  # next N member searches show a dismissable system notice
    session_ttl_s: int = 900


def create_app(variant: str = "pinnacle") -> FastAPI:
    v = VARIANTS[variant]
    app = FastAPI(title=f"CoreOne Teller ({v.institution})", docs_url=None, redoc_url=None)
    templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
    templates.env.globals.update(v=v)
    templates.env.filters["money"] = lambda d: f"${d:,.2f}"

    store = Store()
    faults = Faults()
    sessions: dict[str, float] = {}  # session id -> expiry (epoch seconds)

    def page(request: Request, name: str, status: int = 200, **ctx: object) -> HTMLResponse:
        return templates.TemplateResponse(request, name, ctx, status_code=status)

    def session_ok(request: Request) -> bool:
        expiry = sessions.get(request.cookies.get(SESSION_COOKIE, ""))
        return expiry is not None and expiry > time.time()

    @app.middleware("http")
    async def latency(request: Request, call_next):  # type: ignore[no-untyped-def]
        if faults.latency_ms and not request.url.path.startswith("/__admin"):
            await asyncio.sleep(faults.latency_ms / 1000)
        return await call_next(request)

    def guard(request: Request) -> HTMLResponse | None:
        """Common checks for content screens: session, then injected transient failure."""
        if not session_ok(request):
            return page(request, "login.html", expired=True)
        if faults.transient_failures > 0:
            faults.transient_failures -= 1
            return page(request, "unavailable.html", status=503)
        return None

    # --- entry, auth, frame shell -------------------------------------------------------

    @app.get("/")
    def root() -> Response:
        return RedirectResponse("/main.html")

    @app.get("/login")
    def login_form(request: Request) -> HTMLResponse:
        return page(request, "login.html")

    @app.post("/login")
    def login(request: Request, userid: str = Form(""), passwd: str = Form("")) -> Response:
        if (userid, passwd) != (DEMO_USER, DEMO_PASSWORD):
            return page(request, "login.html", status=401, bad_login=True)
        sid = secrets.token_hex(16)
        sessions[sid] = time.time() + faults.session_ttl_s
        response = RedirectResponse("/main.html", status_code=303)
        response.set_cookie(SESSION_COOKIE, sid, httponly=True)
        return response

    @app.get("/main.html")
    def main(request: Request) -> Response:
        if not session_ok(request):
            return RedirectResponse("/login")
        return page(request, "main.html")

    @app.get("/banner")
    def banner(request: Request) -> HTMLResponse:
        return page(request, "banner.html")

    @app.get("/nav")
    def nav(request: Request) -> HTMLResponse:
        return page(request, "nav.html")

    @app.get("/signoff")
    def signoff(request: Request) -> Response:
        sessions.pop(request.cookies.get(SESSION_COOKIE, ""), None)
        return page(request, "login.html", signed_off=True)

    # --- member inquiry -----------------------------------------------------------------

    @app.get("/inquiry")
    def inquiry_form(request: Request) -> HTMLResponse:
        return guard(request) or page(request, "inquiry.html")

    def detail(request: Request, member_id: str) -> HTMLResponse:
        return page(request, "detail.html", m=store.members[member_id])

    @app.post("/inquiry")
    def inquiry(request: Request, mbrno: str = Form("")) -> HTMLResponse:
        if blocked := guard(request):
            return blocked
        mbrno = mbrno.strip()
        if not (mbrno.isdigit() and len(mbrno) == 5):
            return page(request, "inquiry.html", status=422, mbrno=mbrno,
                        error="Invalid member number. Enter a 5-digit member number.")
        if faults.broadcast_notices > 0:
            faults.broadcast_notices -= 1
            return page(request, "notice.html", mbrno=mbrno)
        if faults.fatal_errors > 0:
            faults.fatal_errors -= 1
            return page(request, "system_error.html", status=500)
        member = store.members.get(mbrno)
        if member is None:
            return page(request, "inquiry.html", mbrno=mbrno, not_found=True)
        if member.restricted:
            return page(request, "denied.html", status=403)
        if member.alert:
            return page(request, "alert.html", m=member)
        return detail(request, mbrno)

    @app.post("/inquiry/ack")
    def acknowledge_alert(request: Request, mbrno: str = Form("")) -> HTMLResponse:
        if blocked := guard(request):
            return blocked
        if mbrno not in store.members:
            return page(request, "inquiry.html", mbrno=mbrno, not_found=True)
        return detail(request, mbrno)

    # --- open sub-account (the irreversible flow) --------------------------------------

    @app.get("/subacct")
    def subaccount_form(request: Request, m: str = "") -> HTMLResponse:
        if blocked := guard(request):
            return blocked
        if m not in store.members:
            return page(request, "inquiry.html", mbrno=m, not_found=True)
        return page(request, "subacct.html", m=store.members[m], types=SUB_ACCOUNT_TYPES)

    @app.post("/subacct/review")
    def subaccount_review(request: Request, m: str = Form(""), accttype: str = Form(""),
                          nick: str = Form(""), initdep: str = Form("")) -> HTMLResponse:
        if blocked := guard(request):
            return blocked
        member = store.members.get(m)
        if member is None:
            return page(request, "inquiry.html", mbrno=m, not_found=True)
        form = {"accttype": accttype, "nick": nick.strip(), "initdep": initdep.strip()}
        error = None
        try:
            deposit = Decimal(form["initdep"].replace(",", "").lstrip("$"))
        except InvalidOperation:
            deposit = Decimal(-1)
        if accttype not in SUB_ACCOUNT_TYPES:
            error = "Select an account type."
        elif deposit < Decimal("5.00"):
            error = "Initial deposit must be at least $5.00."
        if error:
            return page(request, "subacct.html", status=422, m=member, types=SUB_ACCOUNT_TYPES,
                        form=form, error=error)
        token = secrets.token_hex(8)
        store.pending[token] = {**form, "m": m, "initdep": f"{deposit:.2f}"}
        return page(request, "subacct_review.html", m=member, form=store.pending[token],
                    token=token)

    @app.post("/subacct/confirm")
    def subaccount_confirm(request: Request, token: str = Form("")) -> HTMLResponse:
        if blocked := guard(request):
            return blocked
        pending = store.pending.pop(token, None)
        if pending is None:
            return page(request, "system_error.html", status=409,
                        message="This request has already been processed or has expired.")
        member = store.members[pending["m"]]
        amount = Decimal(pending["initdep"])
        suffix = f"{len(member.accounts) * 10:02d}"
        number = f"{7000 + len(member.accounts)}"
        member.accounts.append(Account(suffix, pending["accttype"], number, amount, amount))
        confirmation = f"SA-{store.next_confirmation}"
        store.next_confirmation += 1
        return page(request, "subacct_done.html", m=member, confirmation=confirmation,
                    suffix=suffix, form=pending)

    # --- fault injection (test harness; not part of the "real" app) ---------------------

    @app.get("/__admin/faults")
    def get_faults() -> Faults:
        return faults

    @app.put("/__admin/faults")
    def set_faults(update: Faults) -> Faults:
        for key, value in update.model_dump().items():
            setattr(faults, key, value)
        return faults

    @app.post("/__admin/expire-sessions")
    def expire_sessions() -> dict[str, int]:
        count = len(sessions)
        sessions.clear()
        return {"expired": count}

    @app.post("/__admin/reset")
    def reset() -> dict[str, str]:
        nonlocal store
        store = Store()
        set_faults(Faults())
        sessions.clear()
        return {"status": "reset"}

    return app
