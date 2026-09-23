from collections.abc import Callable, Iterator

import httpx
import pytest

from cua.apps import classify
from cua.schema import AppModel, Target
from cua.schema.targets import ByCss, ByNear, ByRole, ByTableCell, Fingerprint, FrameSelector
from cua.surface import ResolutionError
from cua.surface.web import WebSurface, parse_snapshot
from mock_bank.app import DEMO_PASSWORD, DEMO_USER

CONTENT = [FrameSelector(name="content")]


def target(*strategies, frame=CONTENT, fingerprint=None) -> Target:  # type: ignore[no-untyped-def]
    return Target(frame=frame, strategies=list(strategies), fingerprint=fingerprint)


MEMBER_ID = target(ByNear(text="Member ID", direction="right", role="textbox"))
SEARCH = target(ByRole(role="button", name="Search"))


@pytest.fixture
def surface() -> Iterator[WebSurface]:
    with WebSurface.launch() as s:
        yield s


def sign_on(surface: WebSurface, base_url: str) -> None:
    surface.navigate(f"{base_url}/login")
    surface.fill(surface.resolve(target(
        ByNear(text="User ID", direction="right", role="textbox"), frame=[])), DEMO_USER)
    surface.fill(surface.resolve(target(
        ByNear(text="Password", direction="right", role="textbox"), frame=[])), DEMO_PASSWORD)
    surface.click(surface.resolve(target(ByRole(role="button", name="Sign On"), frame=[])))
    surface.settle()


def search(surface: WebSurface, member_id: str) -> None:
    surface.fill(surface.resolve(MEMBER_ID), member_id)
    surface.click(surface.resolve(SEARCH))
    surface.settle()


def test_parse_snapshot() -> None:
    nodes = parse_snapshot(
        '- cell "Member ID" [ref=f4e10]\n'
        '- link "Sign Off" [ref=f3e15] [cursor=pointer]:\n  - /url: /signoff\n'
        '- paragraph [ref=f4e15]: CoreOne Teller v7.2.3\n'
        "- 'row \"Name: Jane\"':\n  - cell \"Name:\"\n"
    )
    assert [(n.role, n.name, n.ref) for n in nodes[:2]] == [
        ("cell", "Member ID", "f4e10"), ("link", "Sign Off", "f3e15")]
    assert nodes[1].props == {"cursor": "pointer", "url": "/signoff"}
    assert nodes[2].text == "CoreOne Teller v7.2.3"
    assert nodes[3].name == "Name: Jane" and nodes[3].children[0].name == "Name:"


def test_observe_and_classify_across_frames(
    surface: WebSurface, bank: Callable[[str], str], corebank: AppModel
) -> None:
    sign_on(surface, bank("pinnacle"))
    obs = surface.observe()
    assert any(url.endswith("/inquiry") for url in obs.frame_urls)
    assert any(n.role == "textbox" and n.ref for n in obs.nodes())
    assert classify(corebank, obs).state == "member_search"


def test_lookup_resolves_targets_and_extracts(
    surface: WebSurface, bank: Callable[[str], str], corebank: AppModel
) -> None:
    sign_on(surface, bank("pinnacle"))
    search(surface, "12345")
    assert classify(corebank, surface.observe()).state == "member_detail"
    balance = target(ByTableCell(row_has_text="Share Savings", column="Balance"))
    name = target(ByNear(text="Name:", direction="right", role="cell"))
    assert surface.read_text(surface.resolve(balance)) == "$4,210.37"
    assert surface.read_text(surface.resolve(name)) == "Jane Q. Sample"


@pytest.mark.parametrize(
    ("member_id", "fault", "state"),
    [
        ("99999", None, "no_results"),
        ("12a", None, "invalid_member_number"),
        ("34567", None, "access_denied"),
        ("23456", None, "member_alert"),
        ("12345", {"broadcast_notices": 1}, "system_notice"),
        ("12345", {"transient_failures": 1}, "service_unavailable"),
        ("12345", {"fatal_errors": 1}, "system_error"),
    ],
)
def test_runtime_conditions_classify(
    surface: WebSurface, bank: Callable[[str], str], corebank: AppModel,
    member_id: str, fault: dict[str, int] | None, state: str,
) -> None:
    base = bank("pinnacle")
    sign_on(surface, base)
    if fault:
        httpx.put(f"{base}/__admin/faults", json=fault).raise_for_status()
    search(surface, member_id)
    assert classify(corebank, surface.observe()).state == state


def test_session_expiry_classifies(
    surface: WebSurface, bank: Callable[[str], str], corebank: AppModel
) -> None:
    base = bank("pinnacle")
    sign_on(surface, base)
    httpx.post(f"{base}/__admin/expire-sessions").raise_for_status()
    search(surface, "12345")
    assert classify(corebank, surface.observe()).state == "session_expired"


def test_resolution_failures_are_explicit(
    surface: WebSurface, bank: Callable[[str], str]
) -> None:
    sign_on(surface, bank("pinnacle"))
    with pytest.raises(ResolutionError) as ambiguous:
        surface.resolve(target(ByRole(role="cell")))
    assert ambiguous.value.kind == "ambiguous"

    with pytest.raises(ResolutionError) as missing:
        surface.resolve(target(ByRole(role="button", name="Transfer")), timeout_ms=300)
    assert missing.value.kind == "not_found" and missing.value.attempts

    wrong = target(ByRole(role="button", name="Search"), fingerprint=Fingerprint(role="textbox"))
    with pytest.raises(ResolutionError) as mismatch:
        surface.resolve(wrong)
    assert mismatch.value.kind == "fingerprint_mismatch"


def test_fallback_strategy_is_reported_as_drift(
    surface: WebSurface, bank: Callable[[str], str]
) -> None:
    sign_on(surface, bank("pinnacle"))
    resolved = surface.resolve(target(
        ByRole(role="textbox", name="Member ID"),  # unlabelled input: matches nothing
        ByNear(text="Member ID", direction="right", role="textbox"),
    ))
    assert resolved.strategy_index == 1


def _ref(surface: WebSurface, role: str, name: str | None = None) -> str:
    node = next(n for n in surface.observe().nodes()
                if n.role == role and (name is None or n.name == name) and n.ref)
    assert node.ref
    return node.ref


def test_synthesized_targets_are_semantic_and_verified(
    surface: WebSurface, bank: Callable[[str], str]
) -> None:
    sign_on(surface, bank("pinnacle"))

    field = surface.synthesize_target(surface.pin(_ref(surface, "textbox")), "act")
    assert field.frame == CONTENT
    assert field.strategies == [
        ByNear(text="Member ID", direction="right", role="textbox"),
        ByCss(selector='input[name="mbrno"]'),
    ]
    button = surface.synthesize_target(surface.pin(_ref(surface, "button", "Search")), "act")
    assert button.strategies[0] == ByRole(role="button", name="Search")

    search(surface, "12345")
    balance = surface.synthesize_target(surface.pin(_ref(surface, "cell", "$4,210.37")), "extract")
    assert balance.strategies == [ByTableCell(row_has_text="Share Savings", column="Balance")]
    assert balance.fingerprint == Fingerprint(role="cell", tag="td")  # data is not identity
    name = surface.synthesize_target(surface.pin(_ref(surface, "cell", "Jane Q. Sample")),
                                     "extract")
    assert name.strategies[0] == ByNear(text="Name:", direction="right", role="cell")


def test_pinnacle_target_on_riverbend_degrades_to_fallback(
    surface: WebSurface, bank: Callable[[str], str]
) -> None:
    """Same vendor product, different tenant labels: the label anchor misses ("Member #"), the
    selector fallback still resolves, and the drift is visible via strategy_index."""
    sign_on(surface, bank("riverbend"))
    recorded = target(
        ByNear(text="Member ID", direction="right", role="textbox"),
        ByCss(selector='input[name="mbrno"]'),
    )
    assert surface.resolve(recorded).strategy_index == 1
