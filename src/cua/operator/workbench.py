"""The operator workbench: the web app bank staff use. Review and approve what discovery
found, run approved automations with a form, and answer the approvals and handoffs a run
asks for, all without the command line.

It is a front end over `cua.workflows` (the same functions the CLI calls), with a job runner
because a run drives the one live browser this process has. Pages are server-rendered and
plain; everything a person reads is in their words (`present`), never artifact JSON.

Mock scope, as the console: localhost, no authentication, the operator's name is
self-declared (a cookie).
"""

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from cua import workflows
from cua.control import OperatorDesk
from cua.discovery import (
    ClaudeContractProposer,
    ClaudePlanner,
    ContractProposal,
    ContractProposer,
    Goal,
    Planner,
    ProposalError,
)
from cua.schema import Capability, InputSpec, ParamType, Sensitivity, Status
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
    app_id: str = "corebank"  # the application new automations are discovered in
    # The LLM parts, replaceable in tests; by default Claude, which needs ANTHROPIC_API_KEY.
    proposer: ContractProposer | None = None
    planner: Callable[[], Planner] | None = None

    def llm_ready(self) -> bool:
        return bool(self.proposer and self.planner) or bool(os.environ.get("ANTHROPIC_API_KEY"))

    def propose(self) -> ContractProposer:
        return self.proposer or ClaudeContractProposer()

    def new_planner(self) -> Planner:
        return self.planner() if self.planner else ClaudePlanner()

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


SENSITIVITIES = [s.value for s in Sensitivity if s is not Sensitivity.SECRET]
TYPES = [t.value for t in ParamType]


@dataclass
class FieldRow:
    """One input or output row of the contract form, as the person sees and edits it."""

    name: str = ""
    type: str = "string"
    description: str = ""
    sensitivity: str = "internal"
    pattern: str = ""
    choices: str = ""  # comma-separated
    example: str = ""
    alt_example: str = ""


def _rows_from_proposal(proposal: ContractProposal) -> tuple[list[FieldRow], list[FieldRow]]:
    inputs = [FieldRow(n, s.type.value, s.description, s.sensitivity.value, s.pattern or "",
                       ", ".join(s.enum or [])) for n, s in proposal.inputs.items()]
    outputs = [FieldRow(n, s.type.value, s.description, s.sensitivity.value)
               for n, s in proposal.outputs.items()]
    return inputs, outputs


def _rows_from_form(form: dict[str, Any], prefix: str) -> list[FieldRow]:
    rows = []
    for i in range(int(form.get(f"{prefix}count", 0))):
        values = {key: str(form.get(f"{prefix}{i}_{key}", "")).strip()
                  for key in ("remove", *FieldRow.__dataclass_fields__)}
        if values.pop("remove"):
            continue
        row = FieldRow(**values)
        row.type, row.sensitivity = row.type or "string", row.sensitivity or "internal"
        if row.name or row.description:
            rows.append(row)
    return rows


def _goal_from_form(form: dict[str, Any], app_id: str, home: str) -> Goal:
    inputs = {}
    for row in _rows_from_form(form, "in"):
        choices = [c.strip() for c in row.choices.split(",") if c.strip()]
        inputs[row.name] = {
            "type": row.type, "description": row.description or row.name,
            "sensitivity": row.sensitivity, "pattern": row.pattern or None,
            "enum": choices or None, "example": row.example,
            "alt_example": row.alt_example or None}
    outputs = {row.name: {"type": row.type, "description": row.description or row.name,
                          "sensitivity": row.sensitivity}
               for row in _rows_from_form(form, "out")}
    return Goal.model_validate({
        "capability_id": str(form.get("capability_id", "")).strip(),
        "title": str(form.get("title", "")).strip() or None,
        "goal": str(form.get("request", "")).strip(), "app": app_id, "entry": home,
        "inputs": inputs, "outputs": outputs, "max_turns": 25})


def _problem(e: ValidationError) -> str:
    first = e.errors()[0]
    where = ".".join(str(p) for p in first["loc"] if p != "__root__")
    return f"{where}: {first['msg']}" if where else first["msg"]


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

    def screenshots(verification: dict[str, Any] | None
                    ) -> tuple[dict[str, list[str]], list[tuple[str, str]]]:
        """What the checking replays saw: per step, the screenshot after it from each check
        (in check order); and (label, url) for anything else they captured."""
        per_step: dict[str, list[str]] = {}
        other: list[tuple[str, str]] = []
        for n, run in enumerate((verification or {}).get("runs", []), 1):
            for png in sorted(Path(run["evidence_ref"]).glob("snapshots/*.png")):
                url = f"/evidence/{png.resolve().relative_to(config.evidence.resolve())}"
                label = png.stem.split("-", 1)[-1]
                if label.startswith("after-"):
                    per_step.setdefault(label.removeprefix("after-"), []).append(url)
                else:
                    other.append((f"check {n}: {label.replace('-', ' ')}", url))
        return per_step, other

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
        step_shots, shots = screenshots(verification)
        return render(
            request, "automation.html", cap=capability, steps=present.steps(capability),
            ending=present.ending(capability), outcomes=present.outcomes(capability),
            verification=verification, shots=shots, step_shots=step_shots,
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

    # --- new automation -----------------------------------------------------------------

    def examples() -> list[dict[str, Any]]:
        """Existing contracts, for the proposer's naming style: names, types and
        sensitivity only, never values."""
        found = []
        for capability_id in store.capability_ids():
            try:
                cap = store.load(capability_id, status=Status.APPROVED)
            except StoreError:
                continue
            found.append({"id": cap.id, "title": cap.description, "inputs": {
                n: s.model_dump(mode="json", exclude_none=True) for n, s in cap.inputs.items()},
                "outputs": {n: s.model_dump(mode="json") for n, s in cap.outputs.items()}})
        return found

    def contract_page(request: Request, *, request_text: str, title: str, capability_id: str,
                      inputs: list[FieldRow], outputs: list[FieldRow],
                      questions: list[str] = [], warnings: list[str] = [],  # noqa: B006
                      problem: str | None = None) -> HTMLResponse:
        exists = capability_id in store.capability_ids()
        return render(request, "new_contract.html", request_text=request_text, title=title,
                      capability_id=capability_id, inputs=[*inputs, FieldRow()],
                      outputs=[*outputs, FieldRow()], questions=questions, warnings=warnings,
                      problem=problem, exists=exists, types=TYPES,
                      sensitivities=SENSITIVITIES)

    @app.get("/new", response_class=HTMLResponse)
    def new(request: Request) -> HTMLResponse:
        return render(request, "new.html", request_text="", llm_ready=config.llm_ready())

    @app.post("/new", response_class=HTMLResponse)
    def propose(request: Request, request_text: str = Form("")) -> HTMLResponse:
        request_text = request_text.strip()
        if not request_text:
            return render(request, "new.html", request_text="", llm_ready=config.llm_ready(),
                          problem="Describe what the automation should do.")
        app_model = store.app(config.app_id)
        try:
            proposal = config.propose()(request_text, config.app_id, app_model.description,
                                        examples())
        except ProposalError as e:
            return render(request, "new.html", request_text=request_text,
                          llm_ready=config.llm_ready(), problem=str(e))
        inputs, outputs = _rows_from_proposal(proposal)
        return contract_page(request, request_text=request_text, title=proposal.title,
                             capability_id=proposal.capability_id, inputs=inputs,
                             outputs=outputs, questions=proposal.questions,
                             warnings=proposal.warnings)

    @app.post("/new/discover", response_class=HTMLResponse)
    async def start_discovery(request: Request) -> Response:
        form = dict(await request.form())
        app_model = store.app(config.app_id)

        def again(problem: str) -> HTMLResponse:
            """The same form, as the person filled it in, with what to fix."""
            return contract_page(
                request, request_text=str(form.get("request", "")),
                title=str(form.get("title", "")),
                capability_id=str(form.get("capability_id", "")),
                inputs=_rows_from_form(form, "in"), outputs=_rows_from_form(form, "out"),
                problem=problem)

        try:
            goal = _goal_from_form(form, config.app_id, app_model.home or "")
        except ValidationError as e:
            return again(_problem(e))
        if not goal.outputs:
            return again("Add at least one thing it gives back.")
        if missing := [n for n, spec in goal.inputs.items() if not spec.example]:
            return again(f"Give an example value for {', '.join(missing)}: discovery tries "
                         "the automation with it.")
        if not app_model.home:
            return again(f"{config.app_id} has no start page configured")

        def work(progress: Progress) -> Any:
            progress(f"Discovering: {goal.title or goal.goal}")
            # Attended: if the assistant asks for help, or an action needs approval, it comes
            # to "Needs you".
            return workflows.discover_goal(config.workspace(), goal, config.new_planner(),
                                           control=desk, progress=progress)

        return start("/new", "discover", f"Discover: {goal.title or goal.capability_id}", work,
                     goal.capability_id)

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
