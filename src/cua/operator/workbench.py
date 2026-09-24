"""The operator workbench: the web app bank staff use. Review and approve what discovery
found, run approved automations with a form, and answer the approvals and handoffs a run
asks for, all without the command line.

It is a front end over `cua.workflows` (the same functions the CLI calls), with a job runner
because a run drives the one live browser this process has. Pages are server-rendered and
plain; everything a person reads is in their words (`present`), never artifact JSON.

Mock scope, as the console: localhost, no authentication, the operator's name is
self-declared (a cookie).
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from cua import workflows
from cua.control import OperatorDesk
from cua.schema import Capability, InputSpec, Status
from cua.store import Store, StoreError
from cua.workflows import Progress, WorkflowError, Workspace

from . import present
from .console import add_desk_routes, desk_page
from .jobs import Busy, JobRunner

TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")


@dataclass
class WorkbenchConfig:
    store: Store
    evidence: Path
    base_url: str  # the default tenant: capabilities run as recorded
    tenant_urls: dict[str, str] = field(default_factory=dict)  # tenants with overlays
    headed: bool = True  # handoffs happen in the automation's browser window

    def workspace(self, tenant: str | None = None) -> Workspace:
        url = self.tenant_urls[tenant] if tenant else self.base_url
        return Workspace(self.store, self.evidence, url, self.headed)


def _field_label(name: str, spec: InputSpec) -> str:
    return spec.description or name.replace("_", " ")


def _verification(record: dict[str, Any] | None) -> dict[str, Any] | None:
    """A verification record, with the single-run form written before two-input
    verification existed read as a list of one run."""
    if record is None or "runs" in record:
        return record
    runs = [{k: record[k] for k in ("kind", "run_id", "evidence_ref")}] if "run_id" in record \
        else []
    return {**record, "runs": runs}


def _inputs(form: dict[str, Any], prefix: str, capability: Capability) -> dict[str, str]:
    return {name: str(form.get(f"{prefix}{name}", "")).strip() for name in capability.inputs}


def create_workbench(config: WorkbenchConfig, desk: OperatorDesk | None = None,
                     jobs: JobRunner | None = None) -> FastAPI:
    app = FastAPI(title="Operator workbench", docs_url=None, redoc_url=None)
    desk = desk or OperatorDesk()
    jobs = jobs or JobRunner()
    store = config.store
    add_desk_routes(app, desk, config.evidence)
    app.state.desk, app.state.jobs = desk, jobs

    def render(request: Request, template: str, **context: Any) -> HTMLResponse:
        pending = sum(i["status"] == "pending" for i in desk.state()["items"])
        return TEMPLATES.TemplateResponse(request, template, {
            "operator": request.cookies.get("operator", ""), "pending": pending,
            "current_job": jobs.current(), "message": request.query_params.get("msg"),
            "error": request.query_params.get("err"), **context})

    def back(url: str, *, msg: str | None = None, err: str | None = None) -> RedirectResponse:
        query = f"?msg={quote(msg)}" if msg else f"?err={quote(err)}" if err else ""
        return RedirectResponse(url + query, status_code=303)

    def page_url(capability_id: str, version: str) -> str:
        return f"/automations/{capability_id}/{version}"

    def start(url_on_busy: str, kind: str, title: str, work: Any, subject: str) -> Response:
        try:
            job = jobs.submit(kind, title, work, subject=subject)
        except Busy as e:
            return back(url_on_busy, err=f"{e}. Wait for it to finish, then try again.")
        return RedirectResponse(f"/jobs/{job.id}", status_code=303)

    def screenshots(verification: dict[str, Any] | None) -> list[tuple[str, str]]:
        """(label, url) for the screenshots the verification runs took."""
        shots: list[tuple[str, str]] = []
        for n, run in enumerate((verification or {}).get("runs", []), 1):
            run_dir = Path(run["evidence_ref"])
            for png in sorted(run_dir.glob("snapshots/*.png")):
                rel = png.resolve().relative_to(config.evidence.resolve())
                shots.append((f"check {n}: {png.stem.split('-', 1)[-1].replace('-', ' ')}",
                              f"/evidence/{rel}"))
        return shots

    def tenants_view(capability: Capability) -> list[dict[str, Any]]:
        """The default tenant, then every tenant with an overlay, with run and clearance
        status for each."""
        rows = []
        ws = config.workspace()
        rows.append({"tenant": None, "label": "default (as recorded)", "runnable": True,
                     "stability": store.stability(capability.id, capability.version),
                     "clearance": workflows.unattended_clearance(ws, capability)})
        for overlay in store.overlays(capability.id, capability.version):
            row: dict[str, Any] = {"tenant": overlay.tenant, "label": overlay.tenant,
                                   "overlay": overlay,
                                   "runnable": overlay.status is Status.APPROVED
                                   and overlay.tenant in config.tenant_urls}
            if overlay.status is Status.APPROVED:
                merged = store.load(capability.id, capability.version, tenant=overlay.tenant)
                row["stability"] = store.stability(merged.id, merged.version)
                row["clearance"] = workflows.unattended_clearance(ws, merged)
            rows.append(row)
        return rows

    # --- pages ----------------------------------------------------------------------------

    @app.get("/")
    def home() -> RedirectResponse:
        return RedirectResponse("/automations", status_code=303)

    @app.post("/operator")
    def set_operator(request: Request, name: str = Form("")) -> RedirectResponse:
        response = RedirectResponse(request.headers.get("referer") or "/", status_code=303)
        response.set_cookie("operator", name.strip(), samesite="strict")
        return response

    @app.get("/automations", response_class=HTMLResponse)
    def automations(request: Request) -> HTMLResponse:
        rows = []
        for capability_id in store.capability_ids():
            versions = store.versions(capability_id)
            latest = store.load(capability_id, versions[-1])
            try:
                approved: Capability | None = store.load(capability_id, status=Status.APPROVED)
            except StoreError:
                approved = None
            review = None
            if latest.status is Status.DRAFT:
                verification = store.verification(capability_id, latest.version) or {}
                review = ("rejected" if store.rejection(capability_id, latest.version)
                          else "ready for review" if verification.get("kind") == "success"
                          else "verification failed")
            rows.append({
                "id": capability_id, "description": latest.description,
                "latest": latest, "review": review, "approved": approved,
                "cleared": approved is not None
                and not workflows.unattended_clearance(config.workspace(), approved),
                "tenants": [o.tenant for o in store.overlays(capability_id, approved.version)
                            if o.status is Status.APPROVED] if approved else [],
            })
        return render(request, "automations.html", rows=rows)

    @app.get("/automations/{capability_id}")
    def automation_latest(capability_id: str) -> RedirectResponse:
        versions = store.versions(capability_id)
        if not versions:
            return back("/automations", err=f"no automation {capability_id}")
        return RedirectResponse(page_url(capability_id, versions[-1]), status_code=303)

    @app.get("/automations/{capability_id}/{version}", response_class=HTMLResponse)
    def automation(request: Request, capability_id: str, version: str) -> Response:
        try:
            capability = store.load(capability_id, version)
        except StoreError as e:
            return back("/automations", err=str(e))
        verification = _verification(store.verification(capability_id, version))
        return render(
            request, "automation.html", cap=capability, steps=present.steps(capability),
            ending=present.ending(capability), outcomes=present.outcomes(capability),
            verification=verification, shots=screenshots(verification),
            notes=(verification or {}).get("review_notes", []),
            rejection=store.rejection(capability_id, version),
            versions=store.versions(capability_id),
            tenants=tenants_view(capability) if capability.status is Status.APPROVED else [],
            input_fields=[(n, _field_label(n, s), s) for n, s in capability.inputs.items()])

    @app.post("/automations/{capability_id}/{version}/approve")
    async def approve(request: Request, capability_id: str, version: str) -> RedirectResponse:
        form = await request.form()
        url = page_url(capability_id, version)
        try:
            workflows.approve(config.workspace(), capability_id, version,
                              str(form.get("reviewer", "")),
                              read_only_steps=[str(s) for s in form.getlist("read_only")])
        except (StoreError, WorkflowError) as e:
            return back(url, err=str(e))
        return back(url, msg="Approved. It can now be run by a person; measure its stability "
                             "to let it run unattended.")

    @app.post("/automations/{capability_id}/{version}/reject")
    def reject(capability_id: str, version: str, reviewer: str = Form(""),
               reason: str = Form("")) -> RedirectResponse:
        url = page_url(capability_id, version)
        if not reviewer.strip() or not reason.strip():
            return back(url, err="a rejection needs your name and a reason")
        try:
            store.reject(capability_id, version, reviewer.strip(), reason.strip())
        except StoreError as e:
            return back(url, err=str(e))
        return back(url, msg="Rejected. Run discovery again to get a new draft.")

    @app.post("/automations/{capability_id}/{version}/stability")
    async def stability(request: Request, capability_id: str, version: str) -> Response:
        form = await request.form()
        url = page_url(capability_id, version)
        tenant = str(form.get("tenant") or "") or None
        try:
            capability = store.load(capability_id, version, tenant=tenant)
        except StoreError as e:
            return back(url, err=str(e))
        param_sets = [s for s in (_inputs(dict(form), "a_", capability),
                                  _inputs(dict(form), "b_", capability)) if any(s.values())]
        runs = int(str(form.get("runs") or 10))

        def work(progress: Progress) -> Any:
            return workflows.measure_stability(config.workspace(tenant), capability,
                                               param_sets, runs=runs, progress=progress)

        return start(url, "stability", f"Measure stability: {capability.description}", work,
                     capability_id)

    @app.get("/run/{capability_id}", response_class=HTMLResponse)
    def run_form(request: Request, capability_id: str, tenant: str | None = None) -> Response:
        try:
            capability = workflows.load_for_replay(config.workspace(), capability_id,
                                                   tenant=tenant)
        except StoreError as e:
            return back("/automations", err=str(e))
        return render(request, "run.html", cap=capability, tenant=tenant,
                      steps=present.steps(capability),
                      input_fields=[(n, _field_label(n, s), s)
                                    for n, s in capability.inputs.items()])

    @app.post("/run/{capability_id}")
    async def run(request: Request, capability_id: str) -> Response:
        form = await request.form()
        tenant = str(form.get("tenant") or "") or None
        url = f"/run/{capability_id}" + (f"?tenant={tenant}" if tenant else "")
        try:
            capability = workflows.load_for_replay(config.workspace(tenant), capability_id,
                                                   tenant=tenant)
        except StoreError as e:
            return back(url, err=str(e))
        inputs = _inputs(dict(form), "in_", capability)

        def work(progress: Progress) -> Any:
            progress(f"Running {capability.description}")
            # A person started it and is at the workbench: approvals and handoffs go to
            # "Needs you" instead of failing closed.
            return capability, workflows.replay_capability(
                config.workspace(tenant), capability, inputs, control=desk, attended=True)

        return start(url, "run", capability.description, work, capability_id)

    @app.get("/jobs", response_class=HTMLResponse)
    def job_list(request: Request) -> HTMLResponse:
        return render(request, "jobs.html", jobs=jobs.recent())

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_page(request: Request, job_id: str) -> Response:
        job = jobs.get(job_id)
        if job is None:
            return back("/jobs", err="that job is no longer kept")
        view = None
        if job.status == "done" and job.kind == "run":
            capability, run_result = job.result
            view = present.result(capability, run_result)
        return render(request, "job.html", job=job, view=view)

    @app.get("/needs-you", response_class=HTMLResponse)
    def needs_you(request: Request) -> HTMLResponse:
        nav = TEMPLATES.get_template("_nav.html").render(
            operator=request.cookies.get("operator", ""), active="needs-you",
            pending=sum(i["status"] == "pending" for i in desk.state()["items"]),
            current_job=jobs.current())
        return HTMLResponse(desk_page(nav))

    return app
