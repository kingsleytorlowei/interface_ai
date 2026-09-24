// "Needs you": the approvals and handoffs a run is waiting on, refreshed every second.
// Shared by the workbench and the per-run console. The operator's name comes from the page's
// name box, or the one the workbench remembered.
(() => {
  const items = document.getElementById("desk-items");
  const holder = document.getElementById("desk-holder");
  const operator = () =>
    (document.getElementById("opname")?.value || localStorage.getItem("operator") || "").trim();

  const el = (tag, attrs = {}, ...children) => {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else node.setAttribute(key, value);
    }
    for (const child of children) if (child) node.append(child);
    return node;
  };

  async function post(url, body) {
    const response = await fetch(url, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ ...body, operator: operator() }),
    });
    if (!response.ok) alert((await response.json()).detail);
    refresh();
  }

  function card(item) {
    const pending = item.status === "pending";
    const title = item.kind === "approval" ? "Approval needed" : "A person is needed";
    const box = el("div", { class: "panel" },
      el("p", {}, el("span", { class: `status ${pending ? "warn" : "plain"}`,
                               text: pending ? title : `${item.status} by ${item.operator || "nobody"}` })),
      el("p", { text: item.reason }));
    if (item.kind === "approval" && item.action)
      box.append(el("p", {}, el("span", { class: `effect ${item.risk}`, text: item.risk }), ` ${item.action}`));
    box.append(el("p", { class: "small soft", text: `run ${item.run_id}, step ${item.step_id || "none"}` }));
    if (pending) {
      const actions = el("div", { style: "display:flex; gap:8px; flex-wrap:wrap; align-items:center" });
      if (item.kind === "approval") {
        const yes = el("button", { class: "primary", text: "Approve" });
        const no = el("button", { text: "Reject" });
        yes.onclick = () => post(`/api/approvals/${item.id}`, { approve: true });
        no.onclick = () => post(`/api/approvals/${item.id}`, { approve: false });
        actions.append(yes, no);
      } else {
        box.append(el("p", { text: "Work in the automation's teller window, then:" }));
        const note = el("input", { type: "text", placeholder: "note (optional)", "aria-label": "note",
                                   id: `note-${item.id}`, style: "flex:1; min-width:200px" });
        const resume = el("button", { class: "primary", text: "Hand back (resume)" });
        const abort = el("button", { text: "End run (abort)" });
        resume.onclick = () => post(`/api/interventions/${item.id}`, { outcome: "resumed", note: note.value });
        abort.onclick = () => post(`/api/interventions/${item.id}`, { outcome: "aborted", note: note.value });
        actions.append(note, resume, abort);
      }
      box.append(actions);
      if (item.evidence_ref)
        box.append(el("img", { src: `/evidence/${item.evidence_ref}.png`, alt: "what the automation saw",
                               style: "max-width:100%; border:1px solid var(--rule); border-radius:4px; margin-top:12px" }));
    }
    return box;
  }

  async function refresh() {
    const active = document.activeElement;
    if (active && active.id && active.id.startsWith("note-")) return;  // don't eat typing
    const state = await (await fetch("/api/state")).json();
    holder.textContent = state.holder ? `In control: ${state.holder}` + (state.pause_requested ? " (pause requested)" : "") : "";
    items.replaceChildren(...(state.items.length ? state.items.map(card)
      : [el("p", { class: "soft", text: "Nothing needs you right now." })]));
  }

  document.getElementById("desk-pause")?.addEventListener("click", () => post("/api/pause", {}));
  document.getElementById("desk-takeover")?.addEventListener("click", () => {
    if (confirm("Taking over ends the run.")) post("/api/takeover", {});
  });
  refresh();
  setInterval(refresh, 1000);
})();
