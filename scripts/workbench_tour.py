"""Drive the operator workbench end to end, as a bank employee would, and screenshot each
screen into evidence/workbench/. Uses the real model for the contract and discovery, so it
needs ANTHROPIC_API_KEY (about $0.10 and two minutes).

    uv run python -m mock_bank --variant pinnacle --port 8001      # in another terminal
    uv run cua console --headless --port 8765                       # in another terminal
    uv run python scripts/workbench_tour.py
    uv run python scripts/workbench_tour.py --from-run corebank.member.x   # skip 1-4

The request, contract and approval are the tour's; everything else is what the workbench
does. Member data is the mock bank's synthetic members.
"""

import sys
from pathlib import Path

import httpx
from playwright.sync_api import Page, sync_playwright

WORKBENCH = "http://127.0.0.1:8765"
BANK = "http://127.0.0.1:8001"
OUT = Path("evidence/workbench")
OPERATOR = "Kingsley Torlowei"
REQUEST = ("A member called in to confirm which savings account we have on file for them. "
           "Find the member by their member number and tell me the account number shown for "
           "their Share Savings account.")
LONG = 300_000  # ms: discovery, and ten stability runs


def shot(page: Page, name: str) -> None:
    page.screenshot(path=str(OUT / f"{name}.png"), full_page=True)
    print(f"  {name}.png")


def wait_for_job(page: Page) -> None:
    """The job page refreshes itself while running; wait until it stops."""
    page.wait_for_function("!document.querySelector('meta[http-equiv=refresh]')", timeout=LONG)


def main() -> int:
    """With `--from-run <capability id>`, start at running an automation that the earlier
    steps already discovered, approved and measured."""
    resume = sys.argv[2] if sys.argv[1:2] == ["--from-run"] else None
    OUT.mkdir(parents=True, exist_ok=True)
    httpx.post(f"{BANK}/__admin/reset").raise_for_status()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(f"{WORKBENCH}/automations")
        page.fill("#opname", OPERATOR)
        page.click("nav.wb button")

        automation = f"/automations/{resume}" if resume else discover_approve_measure(page)
        if automation is None:
            return 1
        run_and_answer(page, automation)
        browser.close()
    return 0


def discover_approve_measure(page: Page) -> str | None:
    print("1. a request in plain English")
    page.goto(f"{WORKBENCH}/new")
    page.fill("#req", REQUEST)
    shot(page, "01-new-automation")
    page.click("text=Suggest what it needs")
    page.wait_for_selector("text=Check what it needs and gives back", timeout=120_000)

    print("2. the proposed contract, with example values added")
    page.fill("input[name=in0_example]", "12345")
    page.fill("input[name=in0_alt_example]", "45678")
    shot(page, "02-proposed-contract")
    page.click("text=Find how to do it")
    page.wait_for_selector("text=Working", timeout=30_000)
    page.wait_for_timeout(15_000)
    shot(page, "03-discovery-in-progress")
    wait_for_job(page)
    shot(page, "04-discovery-done")
    if not page.is_visible("text=Review the draft"):
        print("discovery didn't produce an approvable draft; see the page")
        return None

    print("3. review and approve")
    page.click("text=Review the draft")
    shot(page, "05-review-draft")
    for box in page.query_selector_all("input[name=read_only]"):
        box.check()  # the only lowerable step is the search click, which only queries
    page.click("button:has-text('Approve')")
    page.wait_for_selector("text=Approved.")
    shot(page, "06-approved")

    print("4. measure stability, which clears it to run unattended")
    page.fill("input[name=a_member_id]", "12345")
    page.fill("input[name=b_member_id]", "45678")
    page.click("button:has-text('Measure')")
    wait_for_job(page)
    shot(page, "07-stability")
    return page.get_attribute("text=the automation", "href")


def run_and_answer(page: Page, automation: str) -> None:
    print("5. run it with a form")
    page.goto(f"{WORKBENCH}{automation}")
    page.click("a[href^='/run/']")
    page.fill("input[name=in_member_id]", "12345")
    shot(page, "08-run-form")
    page.click("button:has-text('Run')")
    wait_for_job(page)
    shot(page, "09-run-done")

    print("6. a business outcome, in the automation's own words")
    page.goto(f"{WORKBENCH}{automation}")
    page.click("a[href^='/run/']")
    page.fill("input[name=in_member_id]", "99999")
    page.click("button:has-text('Run')")
    wait_for_job(page)
    shot(page, "10-run-not-found")

    print("7. a run that needs a person: the member alert goes to 'Needs you'")
    page.goto(f"{WORKBENCH}/run/corebank.member.lookup_balance")
    page.fill("input[name=in_member_id]", "23456")
    page.click("button:has-text('Run')")
    page.wait_for_selector("text=It needs you", timeout=60_000)
    page.goto(f"{WORKBENCH}/needs-you")
    page.wait_for_selector("text=Hand back (resume)")
    shot(page, "11-needs-you")
    # This tour's browser is headless, so nobody can acknowledge the alert in it; the
    # person ends the run instead (see evidence/console/ for a real handoff).
    page.fill("input[placeholder='note (optional)']", "tour: no window to act in")
    page.click("text=End run (abort)")
    page.goto(f"{WORKBENCH}/jobs")
    page.click("table a >> nth=0")
    wait_for_job(page)
    shot(page, "12-run-stopped")

    page.goto(f"{WORKBENCH}/automations")
    shot(page, "13-automations")


if __name__ == "__main__":
    sys.exit(main())
