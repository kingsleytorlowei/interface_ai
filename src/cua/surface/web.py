"""Web surface adapter: Playwright + Chromium.

Perception uses Playwright's AI-mode aria snapshot: an accessibility tree spanning all frames,
with refs, computed by the same role/name engine that resolves `get_by_role` — so what the LLM
sees and what replay resolves share one vocabulary (Chromium's raw CDP tree does not: it reports
layout tables as `LayoutTableCell` where Playwright says `cell`).

Strategies Playwright lacks (`near`, `table_cell`) are computed in-page from geometry and table
structure. Nothing here writes to the application's DOM.
"""

import json
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlparse

import yaml
from playwright.sync_api import ElementHandle, Frame, Locator, Page, sync_playwright
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from cua.schema import ElementInfo, HumanAction, Observation, Target, UINode
from cua.schema.targets import (
    ByCss,
    ByLabel,
    ByNear,
    ByRole,
    ByTableCell,
    ByText,
    Fingerprint,
    FrameSelector,
    Strategy,
)

from .base import ActionTimeout, Pinned, ResolutionError, Resolved

# --- snapshot parsing ------------------------------------------------------------------------

_KEY_RE = re.compile(r'^(?P<role>[\w-]+)(?: "(?P<name>(?:[^"\\]|\\.)*)")?(?P<attrs>(?: \[[^\]]+\])*)$')
_ATTR_RE = re.compile(r"\[([^\]=]+)(?:=([^\]]*))?\]")


def parse_snapshot(snapshot: str) -> list[UINode]:
    """Parse Playwright's YAML aria snapshot into `UINode`s."""
    try:
        data = yaml.safe_load(snapshot) or []
    except yaml.YAMLError:
        return []
    return [node for item in data if (node := _parse_item(item)) is not None]


def _parse_item(item: Any) -> UINode | None:
    if isinstance(item, str):
        key, value = item, None
    elif isinstance(item, dict) and len(item) == 1:
        key, value = next(iter(item.items()))
    else:
        return None
    key = str(key).strip()
    if key.startswith("/"):
        return None  # a property line (e.g. `/url`), folded into the parent below
    match = _KEY_RE.match(key)
    if match is None:
        return UINode(role="text", name=key)
    props = {k: v or "" for k, v in _ATTR_RE.findall(match["attrs"] or "")}
    ref = props.pop("ref", None)
    name = (match["name"] or "").replace('\\"', '"').replace("\\\\", "\\")
    text, children = None, []
    if isinstance(value, list):
        for child in value:
            if isinstance(child, dict) and len(child) == 1:
                k, v = next(iter(child.items()))
                if str(k).startswith("/"):
                    props[str(k)[1:]] = str(v)
                    continue
            if (node := _parse_item(child)) is not None:
                children.append(node)
    elif value is not None:
        text = str(value)
    return UINode(role=match["role"], name=name, ref=ref, text=text, props=props,
                  children=children)


# --- in-page scripts -------------------------------------------------------------------------

_NORM = "const norm = s => (s || '').replace(/\\s+/g, ' ').trim();"
# A label worth anchoring on: short, has letters, no digits (digits usually mean data).
_STABLE = "const stable = t => t.length > 0 && t.length <= 40 && /[A-Za-z]/.test(t) && !/\\d/.test(t);"

_ATTRS_JS = """e => ({
  tag: e.tagName.toLowerCase(),
  type: e.tagName === 'INPUT' ? (e.getAttribute('type') || 'text').toLowerCase() : null,
  href: e.tagName === 'A' && e.hasAttribute('href') ? e.href : null,
})"""

# Installed into every frame at launch. Records human clicks and field changes into
# sessionStorage (it survives the form posts that navigate a frame), but only while the
# capture flag is set, i.e. while the engine has handed the session to a person.
_CAPTURE_JS = """(() => {
  if (window.__cuaCapture) return;
  window.__cuaCapture = true;
  const norm = s => (s || '').replace(/\\s+/g, ' ').trim().slice(0, 80);
  const roleOf = (el, tag, type) => el.getAttribute('role') || (
    tag === 'a' ? 'link' : tag === 'select' ? 'combobox' :
    (tag === 'button' || ['submit', 'button', 'reset'].includes(type)) ? 'button' :
    (tag === 'input' || tag === 'textarea') ? 'textbox' : tag);
  const describe = el => {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    const name = el.getAttribute('aria-label')
      || (tag === 'input' && ['submit', 'button'].includes(type) ? el.value : '')
      || (el.labels && el.labels[0] ? el.labels[0].innerText : '')
      || (['input', 'select', 'textarea'].includes(tag) ? '' : el.innerText)
      || el.getAttribute('name') || '';
    return `${roleOf(el, tag, type)} "${norm(name)}"`;
  };
  const record = (kind, el, value) => {
    try {
      if (sessionStorage.getItem('cua.capture') !== '1') return;
      const list = JSON.parse(sessionStorage.getItem('cua.captured') || '[]');
      list.push({kind, target: describe(el), value, at: Date.now()});
      sessionStorage.setItem('cua.captured', JSON.stringify(list));
    } catch (e) {}
  };
  const FIELD = 'input:not([type]),input[type=text],input[type=password],textarea,select';
  document.addEventListener('click', e => {
    const el = e.target.closest('a,button,input,select,textarea,[role]') || e.target;
    if (!el.matches(FIELD)) record('click', el, null);  // a field's change is the action
  }, true);
  document.addEventListener('change', e => {
    const el = e.target;
    if (!el.matches || !el.matches(FIELD)) return;
    const secret = (el.getAttribute('type') || '').toLowerCase() === 'password';
    const value = secret ? '***'
      : el.tagName === 'SELECT' ? (el.options[el.selectedIndex] || {}).text : el.value;
    record(el.tagName === 'SELECT' ? 'select' : 'fill', el, value);
  }, true);
})()"""

_CAPTURE_START = """() => { sessionStorage.setItem('cua.captured', '[]');
  sessionStorage.setItem('cua.capture', '1'); }"""
_CAPTURE_DRAIN = """() => { const list = JSON.parse(sessionStorage.getItem('cua.captured') || '[]');
  sessionStorage.removeItem('cua.captured'); sessionStorage.removeItem('cua.capture');
  return list; }"""

_BODY_TEXT = "() => document.body ? document.body.innerText : ''"

_DESCRIBE_JS = f"""(el) => {{
  {_NORM} {_STABLE}
  const r = el.getBoundingClientRect();
  const anchors = [];
  for (const e of document.body.querySelectorAll('*')) {{
    if (e === el || e.contains(el) || el.contains(e) || e.children.length) continue;
    if (['SCRIPT', 'STYLE', 'OPTION'].includes(e.tagName)) continue;
    const text = norm(e.innerText);
    const b = e.getBoundingClientRect();
    if (!stable(text) || !b.width || !b.height) continue;
    const overlapV = b.top < r.bottom && b.bottom > r.top;
    const overlapH = b.left < r.right && b.right > r.left;
    if (overlapV && b.right <= r.left + 2) anchors.push({{text, direction: 'right', dist: r.left - b.right}});
    else if (overlapH && b.bottom <= r.top + 2) anchors.push({{text, direction: 'below', dist: r.top - b.bottom}});
  }}
  // A field's label sits to its left or, failing that, above it; nearest-overall would pick
  // section headers over labels.
  anchors.sort((a, b) => a.dist - b.dist);
  const left = anchors.filter(a => a.direction === 'right');
  const labels = left.length ? left : anchors;
  let table = null;
  if (el.tagName === 'TD' || el.tagName === 'TH') {{
    const rows = Array.from(el.closest('table').rows);
    const header = rows.find(row => Array.from(row.cells).some(c => c.tagName === 'TH'));
    if (header && el.parentElement !== header) {{
      const column = norm(header.cells[el.cellIndex] && header.cells[el.cellIndex].innerText);
      const unique = t => rows.filter(row => Array.from(row.cells).some(c => norm(c.innerText) === t)).length === 1;
      const key = Array.from(el.parentElement.cells).map(c => c === el ? '' : norm(c.innerText))
        .find(t => stable(t) && unique(t));
      if (column && key) table = {{rowKey: key, column}};
    }}
  }}
  return {{tag: el.tagName.toLowerCase(), id: el.id || null, nameAttr: el.getAttribute('name'),
           anchors: labels.slice(0, 2), table}};
}}"""

_NEAR_JS = """(els, {anchor, direction}) => {
  const a = anchor.getBoundingClientRect();
  const scored = [];
  els.forEach((e, i) => {
    if (e.contains(anchor) || anchor.contains(e)) return;
    const b = e.getBoundingClientRect();
    const overlapV = b.top < a.bottom && b.bottom > a.top;
    const overlapH = b.left < a.right && b.right > a.left;
    let d = null;
    if (direction === 'right' && overlapV && b.left >= a.right - 2) d = b.left - a.right;
    if (direction === 'left' && overlapV && b.right <= a.left + 2) d = a.left - b.right;
    if (direction === 'below' && overlapH && b.top >= a.bottom - 2) d = b.top - a.bottom;
    if (direction === 'above' && overlapH && b.bottom <= a.top + 2) d = a.top - b.bottom;
    if (d !== null) scored.push([d, i]);
  });
  scored.sort((x, y) => x[0] - y[0]);
  if (!scored.length) return {index: -1, count: 0};
  return {index: scored[0][1], count: scored.filter(s => s[0] - scored[0][0] < 1).length};
}"""

_TABLE_CELL_JS = f"""({{rowText, column}}) => {{
  {_NORM}
  const xpath = el => {{
    const parts = [];
    for (let n = el; n && n.nodeType === 1; n = n.parentElement) {{
      let i = 1;
      for (let s = n.previousElementSibling; s; s = s.previousElementSibling) if (s.tagName === n.tagName) i++;
      parts.unshift(n.tagName.toLowerCase() + '[' + i + ']');
    }}
    return '/' + parts.join('/');
  }};
  const out = [];
  for (const table of document.querySelectorAll('table')) {{
    const rows = Array.from(table.rows);
    const header = rows.find(row => Array.from(row.cells).some(c => c.tagName === 'TH'));
    if (!header) continue;
    const idx = Array.from(header.cells).findIndex(c => norm(c.innerText) === column);
    if (idx < 0) continue;
    for (const row of rows) {{
      if (row === header || !row.cells[idx]) continue;
      if (Array.from(row.cells).some(c => norm(c.innerText) === rowText)) out.push(xpath(row.cells[idx]));
    }}
  }}
  return out;
}}"""


def describe_strategy(s: Strategy) -> str:
    return f"{s.by}{json.dumps(s.model_dump(exclude={'by'}, exclude_none=True))}"


# --- adapter ---------------------------------------------------------------------------------


# How long one action (navigation, click, fill, ...) may take, including the page load it
# triggers, before it counts as a timeout. Playwright's own default is 30 s, which turns one
# hung load into a minute-long run; a checkpoint's own wait is separate (Step.expect).
ACTION_TIMEOUT_MS = 10_000
SNAPSHOT_TIMEOUT_MS = 5_000  # failure evidence must not hang on the page that failed


@contextmanager
def _action_budget() -> Iterator[None]:
    try:
        yield
    except PlaywrightTimeout as e:
        raise ActionTimeout(str(e).splitlines()[0]) from e


class WebSurface:
    def __init__(self, page: Page) -> None:
        self.page = page
        self._inflight = 0
        self._last_activity = time.monotonic()
        self._last_obs: Observation | None = None
        page.on("request", lambda _: self._activity(+1))
        page.on("requestfinished", lambda _: self._activity(-1))
        page.on("requestfailed", lambda _: self._activity(-1))

    @classmethod
    @contextmanager
    def launch(cls, *, headless: bool = True) -> Iterator["WebSurface"]:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=headless)
            context = browser.new_context(viewport={"width": 1280, "height": 800})
            context.set_default_timeout(ACTION_TIMEOUT_MS)
            context.add_init_script(_CAPTURE_JS)
            try:
                yield cls(context.new_page())
            finally:
                browser.close()

    def _activity(self, delta: int = 0) -> None:
        self._inflight = max(0, self._inflight + delta)
        self._last_activity = time.monotonic()

    # perception -------------------------------------------------------------------------

    def settle(self, timeout_ms: int = 5000, quiet_ms: int = 300) -> None:
        """Heuristic quiescence: no requests in flight, all frames loaded, and nothing new for
        `quiet_ms` since the last request *or action* (a click's navigation request may not have
        started yet when the click returns). Replay does not rely on this alone: it waits for
        the step's checkpoint state."""
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            quiet = time.monotonic() - self._last_activity >= quiet_ms / 1000
            if self._inflight == 0 and quiet and self._frames_ready():
                return
            self.page.wait_for_timeout(50)

    def _frames_ready(self) -> bool:
        try:
            return all(f.evaluate("() => document.readyState") == "complete"
                       for f in self._frames())
        except PlaywrightError:
            return False  # a frame is mid-navigation

    def observe(self, settle_timeout_ms: int = 5000) -> Observation:
        for attempt in range(3):
            self.settle(settle_timeout_ms)
            try:
                snapshot = self.page.aria_snapshot(mode="ai", timeout=SNAPSHOT_TIMEOUT_MS)
                text = "\n".join(f.evaluate(_BODY_TEXT) for f in self._frames())
                break
            except PlaywrightTimeout as e:
                if attempt == 2:
                    raise ActionTimeout(str(e).splitlines()[0]) from e
            except PlaywrightError:
                if attempt == 2:
                    raise
        self._last_obs = Observation(
            url=self.page.url,
            title=self.page.title(),
            frame_urls=[f.url for f in self._frames()[1:]],
            text=text,
            tree=parse_snapshot(snapshot),
            snapshot=snapshot,
        )
        return self._last_obs

    def screenshot(self) -> bytes:
        masks = [f.locator("input[type=password]") for f in self._frames()]
        return self.page.screenshot(mask=masks, timeout=SNAPSHOT_TIMEOUT_MS)

    def navigate(self, url: str) -> None:
        with _action_budget():
            self.page.goto(url)
        self.settle()

    # frames -----------------------------------------------------------------------------

    def _frames(self) -> list[Frame]:
        """Live frames, main frame first. Playwright keeps frames detached by a reload of the
        frameset in its lists; they must never be matched, read or waited on."""
        return [f for f in self.page.frames if not f.is_detached()]

    @staticmethod
    def _frame_path(frame: Frame) -> list[FrameSelector]:
        path: list[FrameSelector] = []
        while frame.parent_frame is not None:
            if frame.name:
                path.append(FrameSelector(name=frame.name))
            else:
                path.append(FrameSelector(url_contains=urlparse(frame.url).path))
            frame = frame.parent_frame
        return path[::-1]

    @staticmethod
    def _frame_matches(frame: Frame, sel: FrameSelector) -> bool:
        if sel.name is not None and frame.name != sel.name:
            return False
        if sel.url_contains is not None and sel.url_contains not in frame.url:
            return False
        if sel.title is not None:
            return frame.frame_element().get_attribute("title") == sel.title
        return True

    def _find_frame(self, path: list[FrameSelector]) -> Frame:
        frame = self.page.main_frame
        for sel in path:
            matches = [c for c in frame.child_frames
                       if not c.is_detached() and self._frame_matches(c, sel)]
            if len(matches) != 1:
                raise ResolutionError(
                    "frame_not_found", f"{len(matches)} frames match {sel.model_dump()}"
                )
            frame = matches[0]
        return frame

    # resolution -------------------------------------------------------------------------

    def _matches(self, frame: Frame, s: Strategy) -> tuple[Locator, int]:
        """Locator for the strategy's match, and how many elements matched."""
        match s:
            case ByRole(role=role, name=name, exact=exact):
                loc = frame.get_by_role(role, name=name, exact=exact)  # type: ignore[arg-type]
            case ByLabel(text=text):
                loc = frame.get_by_label(text, exact=True)
            case ByText(text=text, role=None):
                loc = frame.get_by_text(text, exact=True)
            case ByText(text=text, role=role):
                pattern = re.compile(rf"^\s*{re.escape(text)}\s*$")
                loc = frame.get_by_role(role).filter(has_text=pattern)  # type: ignore[arg-type]
            case ByCss(selector=selector):
                loc = frame.locator(selector).filter(visible=True)
            case ByNear():
                return self._near(frame, s)
            case ByTableCell(row_has_text=row_text, column=column):
                xpaths = frame.evaluate(_TABLE_CELL_JS, {"rowText": row_text, "column": column})
                loc = frame.locator(f"xpath={xpaths[0]}") if xpaths else frame.locator("xpath=/..")
                return loc, len(xpaths)
        return loc, loc.count()

    def _near(self, frame: Frame, s: ByNear) -> tuple[Locator, int]:
        anchors = frame.get_by_text(s.text, exact=True).filter(visible=True)
        if (n := anchors.count()) != 1:
            return anchors, n
        candidates = frame.get_by_role(s.role).filter(visible=True)  # type: ignore[arg-type]
        found = candidates.evaluate_all(
            _NEAR_JS, {"anchor": anchors.element_handle(), "direction": s.direction}
        )
        return candidates.nth(max(found["index"], 0)), found["count"]

    def resolve(self, target: Target, timeout_ms: int = 5000) -> Resolved:
        """First strategy matching exactly one element wins, then the fingerprint must agree.
        Waits (up to the timeout) only while nothing matches; ambiguity fails fast."""
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            attempts: list[str] = []
            ambiguous = False
            try:
                frame = self._find_frame(target.frame)
            except ResolutionError as e:
                if time.monotonic() >= deadline:
                    raise
                attempts.append(e.detail)
                self.page.wait_for_timeout(200)
                continue
            for i, strategy in enumerate(target.strategies):
                label = describe_strategy(strategy)
                try:
                    loc, count = self._matches(frame, strategy)
                except PlaywrightError as e:
                    attempts.append(f"{label} -> error: {e.message.splitlines()[0]}")
                    continue
                attempts.append(f"{label} -> {count} match(es)")
                if count == 1:
                    self._check_fingerprint(loc, target.fingerprint, attempts)
                    return Resolved(loc, i, label)
                ambiguous = ambiguous or count > 1
            if ambiguous:
                raise ResolutionError("ambiguous", "no strategy matched exactly one", attempts)
            if time.monotonic() >= deadline:
                raise ResolutionError("not_found", "no strategy matched", attempts)
            self.page.wait_for_timeout(200)

    def audit(self, target: Target, el: Resolved) -> list[bool]:
        frame = self._find_frame(target.frame)
        handle = el.handle.element_handle()
        return [i == el.strategy_index or self._identifies(frame, s, handle)
                for i, s in enumerate(target.strategies)]

    def describe(self, el: Resolved) -> ElementInfo:
        nodes = parse_snapshot(el.handle.aria_snapshot(mode="ai"))
        attrs = el.handle.evaluate(_ATTRS_JS)
        return ElementInfo(
            role=nodes[0].role if nodes else "",
            name=nodes[0].name if nodes else "",
            tag=attrs["tag"],
            input_type=attrs["type"],
            href=attrs["href"],
        )

    def _check_fingerprint(
        self, loc: Locator, fp: Fingerprint | None, attempts: list[str]
    ) -> None:
        if fp is None:
            return
        nodes = parse_snapshot(loc.aria_snapshot(mode="ai"))
        actual = Fingerprint(
            role=nodes[0].role if nodes else None,
            name=nodes[0].name if nodes else None,
            tag=loc.evaluate("e => e.tagName.toLowerCase()"),
        )
        for field in ("role", "name", "tag"):
            expected = getattr(fp, field)
            if expected is not None and getattr(actual, field) != expected:
                raise ResolutionError(
                    "fingerprint_mismatch",
                    f"expected {fp.model_dump(exclude_none=True)}, got {actual.model_dump()}",
                    attempts,
                )

    # recording --------------------------------------------------------------------------

    def pin(self, ref: str) -> Pinned:
        loc = self.page.locator(f"aria-ref={ref}")
        handle = loc.element_handle(timeout=2000) if loc.count() == 1 else None
        node = self._last_obs.find_ref(ref) if self._last_obs else None
        if handle is None or node is None:
            raise ResolutionError("not_found", f"ref {ref} is not in the latest observation")
        return Pinned(handle=handle, role=node.role, name=node.name)

    def synthesize_target(self, pinned: Pinned, purpose: Literal["act", "extract"]) -> Target:
        handle: ElementHandle = pinned.handle
        frame = handle.owner_frame()
        assert frame is not None
        info = handle.evaluate(_DESCRIBE_JS)

        # Ranked most to least robust. Accessible names of extracted elements are data, not
        # identity, so they only anchor `act` targets.
        candidates: list[Strategy] = []
        if purpose == "act" and pinned.name:
            candidates += [ByRole(role=pinned.role, name=pinned.name),
                           ByLabel(text=pinned.name)]
        if info["table"]:
            candidates.append(ByTableCell(row_has_text=info["table"]["rowKey"],
                                          column=info["table"]["column"]))
        candidates += [ByNear(text=a["text"], direction=a["direction"], role=pinned.role)
                       for a in info["anchors"]]
        if purpose == "act":
            if info["id"]:
                candidates.append(ByCss(selector=f"#{info['id']}"))
            if info["nameAttr"]:
                candidates.append(ByCss(selector=f"{info['tag']}[name=\"{info['nameAttr']}\"]"))

        verified: list[Strategy] = []
        for s in candidates:
            if s not in verified and self._identifies(frame, s, handle):
                verified.append(s)
        if not verified:
            raise ResolutionError("not_found", "no strategy uniquely identifies this element",
                                  [describe_strategy(s) for s in candidates])
        return Target(
            frame=self._frame_path(frame),
            strategies=verified,
            fingerprint=Fingerprint(
                role=pinned.role,
                name=pinned.name if purpose == "act" and pinned.name else None,
                tag=info["tag"],
            ),
        )

    def _identifies(self, frame: Frame, s: Strategy, handle: ElementHandle) -> bool:
        try:
            loc, count = self._matches(frame, s)
            return count == 1 and bool(loc.evaluate("(a, b) => a === b", handle))
        except PlaywrightError:
            return False

    # actions ----------------------------------------------------------------------------

    def click(self, el: Resolved) -> None:
        with _action_budget():
            el.handle.click()
        self._activity()

    def fill(self, el: Resolved, value: str) -> None:
        with _action_budget():
            el.handle.fill(value)
        self._activity()

    def select(self, el: Resolved, option: str) -> None:
        with _action_budget():
            el.handle.select_option(label=option)
        self._activity()

    def press(self, key: str, el: Resolved | None = None) -> None:
        with _action_budget():
            if el is None:
                self.page.keyboard.press(key)
            else:
                el.handle.press(key)
        self._activity()

    # human capture ----------------------------------------------------------------------
    # Same-origin frames share one sessionStorage, so the main frame is enough here.

    def start_capture(self) -> None:
        self.page.main_frame.evaluate(_CAPTURE_START)

    def drain_captured(self) -> list[HumanAction]:
        try:
            raw = self.page.main_frame.evaluate(_CAPTURE_DRAIN)
        except PlaywrightError:
            return []
        return [
            HumanAction(kind=a["kind"], target_description=a["target"], value=a.get("value"),
                        at=datetime.fromtimestamp(a["at"] / 1000, UTC))
            for a in raw
        ]

    def read_text(self, el: Resolved) -> str:
        return " ".join(el.handle.inner_text().split())
