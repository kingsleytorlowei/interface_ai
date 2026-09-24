// The home page's conversation. What you type goes to /api/chat (which takes values out
// before any model sees it); a matching automation comes back as a card to check and run,
// and its result prints as a receipt. Everything is built with DOM calls, never innerHTML.
(() => {
  const form = document.getElementById("ask");
  const box = document.getElementById("message");
  const send = form.querySelector(".send");
  const transcript = document.getElementById("transcript");
  const suggestions = document.getElementById("suggestions");

  // the box grows with what's typed; the arrow only works when there is something to send
  const fit = () => {
    box.style.height = "auto";
    box.style.height = `${Math.min(box.scrollHeight, 200)}px`;
    send.disabled = !box.value.trim();
  };
  const keepInView = () => form.scrollIntoView({ block: "nearest", behavior: "smooth" });

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
  const working = (text) => el("div", { class: "working", text });

  async function post(url, body) {
    const response = await fetch(url, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    return { ok: response.ok, body: await response.json() };
  }

  function receipt(title, result) {
    const slip = el("div", { class: `receipt ${result.tone}`, role: "status" },
      el("div", { class: "head" }, el("span", { text: title }), el("span", { text: result.at })),
      el("div", { class: "verdict", text: result.headline }));
    for (const [label, value] of result.outputs)
      slip.append(el("div", { class: "line" }, el("span", { text: label }), el("span", { text: value })));
    for (const detail of result.details)
      slip.append(el("p", { class: "small soft", style: "margin-top:8px", text: detail }));
    if (result.advice) slip.append(el("div", { class: "advice", text: result.advice }));
    slip.append(el("div", { class: "foot", text: `run ${result.run_id}` }));
    return slip;
  }

  async function follow(jobId, title, where) {
    const status = working("Running…");
    where.append(status);
    let told = false;
    for (;;) {
      await new Promise((resolve) => setTimeout(resolve, 700));
      const response = await fetch(`/api/jobs/${jobId}`);
      const job = await response.json();
      if (job.needs_you && !told) {
        told = true;
        status.replaceWith(el("div", { class: "note" },
          "It needs you: ", el("a", { href: "/needs-you", target: "_blank", text: "open Needs you" }),
          " and act in the teller window, then hand back."));
      }
      if (job.status === "running") continue;
      (told ? where.querySelector(".note") : status)?.remove();
      if (job.result) { where.append(receipt(title, job.result)); keepInView(); }
      else where.append(el("div", { class: "note error", text: job.error || "It didn't finish." }));
      return;
    }
  }

  function runCard(card, where) {
    const fields = el("div", { class: "fields" });
    for (const f of card.fields) {
      const id = `f-${card.id}-${f.name}-${Date.now()}`;
      let input;
      if (f.enum.length) {
        input = el("select", { id, name: f.name });
        for (const option of f.enum) {
          const o = el("option", { text: option });
          if (option === f.value) o.selected = true;
          input.append(o);
        }
      } else {
        input = el("input", { type: "text", id, name: f.name, value: f.value, required: "" });
        if (f.pattern) input.setAttribute("pattern", f.pattern);
        if (f.type === "money") input.setAttribute("inputmode", "decimal");
      }
      fields.append(el("label", { for: id, text: f.label + (f.personal ? " (personal data)" : "") }), input);
    }
    let tenant = null;
    if (card.tenants.length) {
      tenant = el("select", { name: "tenant" }, el("option", { value: "", text: "default institution" }));
      for (const t of card.tenants) tenant.append(el("option", { value: t, text: t }));
      fields.append(el("label", { text: "Institution" }), tenant);
    }
    const run = el("button", { class: "primary", text: "Run" });
    const cardEl = el("form", { class: "runcard" },
      el("h3", { text: card.title }),
      el("div", { class: "small soft",
        text: card.cleared ? "Ready, and cleared to run unattended." : "Ready: a person can run it." }),
      fields, run,
      el("a", { class: "button", href: `/run/${card.id}`, style: "margin-left:8px", text: "Open its page" }));
    cardEl.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!cardEl.reportValidity()) return;
      run.disabled = true;
      const inputs = Object.fromEntries(card.fields.map((f) => [f.name, cardEl.elements[f.name].value]));
      const { ok, body } = await post(`/api/run/${card.id}`, { inputs, tenant: tenant ? tenant.value : "" });
      if (!ok) { where.append(el("div", { class: "note error", text: body.error })); run.disabled = false; return; }
      await follow(body.job, card.title, where);
      run.disabled = false;
    });
    where.append(cardEl);
  }

  function createCard(card, where) {
    if (!card.ready) {
      where.append(el("p", { class: "soft",
        text: "Setting up a new automation needs the assistant (an API key in .env)." }));
      return;
    }
    const setUp = el("form", { method: "post", action: "/new" },
      el("input", { type: "hidden", name: "request_text", value: card.request }),
      el("button", { class: "primary", text: "Set up a new automation" }));
    where.append(el("div", { class: "runcard" },
      el("h3", { text: "New automation" }),
      el("p", { class: "soft", text: card.request }), setUp));
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const message = box.value.trim();
    if (!message) return;
    box.value = "";
    fit();
    suggestions?.remove();
    const exchange = el("div", { class: "exchange" }, el("div", { class: "said", text: message }));
    const thinking = working("Looking…");
    exchange.append(thinking);
    transcript.append(exchange);
    keepInView();
    const { ok, body } = await post("/api/chat", { message });
    thinking.remove();
    if (!ok) { exchange.append(el("div", { class: "note error", text: "That didn't work; try again." })); return; }
    if (body.reply) exchange.append(el("p", { class: "reply", text: body.reply }));
    if (body.card?.type === "run") runCard(body.card, exchange);
    if (body.card?.type === "create") createCard(body.card, exchange);
    keepInView();
  });

  box.addEventListener("input", fit);
  box.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); form.requestSubmit(); }
  });
  for (const example of document.querySelectorAll("[data-example]"))
    example.addEventListener("click", () => { box.value = example.dataset.example; fit(); box.focus(); });
})();
