"""Drive the operator workbench end to end, as a bank employee would, and screenshot each
screen. Uses the real model for the chat, the contract and discovery, so it needs
ANTHROPIC_API_KEY (about $0.15 and three minutes).

    uv run python scripts/demo.py --headless        # in another terminal; note its URL
    uv run python scripts/workbench_tour.py --url http://127.0.0.1:8765 --out evidence/workbench

It asks the chat for a balance (a saved automation, run from the chat), then for something
nothing saved does (a new automation: contract, discovery, review, approval, stability), and
sends a member-alert run to "Needs you". The request, examples and approval are the tour's;
everything else is what the workbench does. Point the workbench at a scratch copy of the
catalog to keep the new automation out of the real one.
"""

import argparse
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

OPERATOR = "Kingsley Torlowei"
NEW_REQUEST = ("Before discussing an account, confirm who we are talking to: find the member "
               "by the member number they give and read back the name and member number shown "
               "on their record.")
LONG = 300_000  # ms: discovery, and ten stability runs


def main() -> int:
    args = argparse.ArgumentParser()
    args.add_argument("--url", default="http://127.0.0.1:8765")
    args.add_argument("--out", type=Path, default=Path("evidence/workbench"))
    opts = args.parse_args()
    opts.out.mkdir(parents=True, exist_ok=True)
    url = opts.url.rstrip("/")

    def shot(page: Page, name: str, full: bool = True) -> None:
        page.screenshot(path=str(opts.out / f"{name}.png"), full_page=full)
        print(f"  {name}.png")

    def wait_for_job(page: Page) -> None:
        """A job page refreshes itself while running; wait until it stops."""
        page.wait_for_function("!document.querySelector('meta[http-equiv=refresh]')",
                               timeout=LONG)

    def ask(page: Page, message: str) -> None:
        page.fill("#message", message)
        page.click("#ask button.primary")
        page.wait_for_function(
            "document.querySelector('.exchange') && !document.querySelector('.exchange .working')",
            timeout=120_000)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(url + "/")
        page.fill("#opname", OPERATOR)
        page.click("form.who button")
        shot(page, "01-home")

        print("1. ask for something a saved automation does, and run it from the chat")
        ask(page, "what's the savings balance for member 45678?")
        shot(page, "02-chat-found-it", full=False)
        page.click(".exchange .runcard button.primary")
        page.wait_for_selector(".exchange .receipt", timeout=120_000)
        page.wait_for_timeout(800)
        shot(page, "03-chat-receipt", full=False)

        print("2. ask for something nothing saved does")
        ask(page, NEW_REQUEST)
        shot(page, "04-chat-new", full=False)
        page.click("text=Set up a new automation")
        page.wait_for_selector("text=Check what it needs and gives back", timeout=120_000)
        page.fill("input[name=in0_example]", "12345")
        page.fill("input[name=in0_alt_example]", "45678")
        shot(page, "05-contract")
        page.click("text=Find how to do it")
        page.wait_for_selector(".working", timeout=30_000)
        page.wait_for_timeout(20_000)
        shot(page, "06-discovering")
        wait_for_job(page)
        shot(page, "07-discovered")
        if not page.is_visible("text=Review the draft"):
            print("discovery didn't produce an approvable draft; see the page")
            return 1

        print("3. review and approve it, then measure its stability")
        page.click("text=Review the draft")
        shot(page, "08-review")
        for box in page.query_selector_all("input[name=read_only]"):
            box.check()  # the only lowerable step is the search click, which only queries
        page.click("button:has-text('Approve')")
        page.wait_for_selector("text=Approved.")
        page.fill("input[name=a_member_id]", "12345")
        page.fill("input[name=b_member_id]", "45678")
        page.click("button:has-text('Measure')")
        wait_for_job(page)
        shot(page, "09-stability")

        print("4. the person in the loop: a member alert goes to 'Needs you'")
        page.goto(url + "/")
        ask(page, "look up member 23456")
        page.click(".exchange .runcard button.primary")
        page.wait_for_selector("text=open Needs you", timeout=60_000)
        shot(page, "10-chat-needs-you", full=False)
        page.goto(url + "/needs-you")
        page.wait_for_selector("text=Hand back (resume)")
        shot(page, "11-needs-you")
        # This browser has no teller window to act in, so the person ends the run instead
        # (evidence/console/ has a real hand-back).
        page.fill("input[aria-label=note]", "tour: no window to act in")
        page.click("text=End run (abort)")
        page.wait_for_timeout(1500)

        page.goto(url + "/")
        shot(page, "12-home-after")
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
