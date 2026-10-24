"""Opens one page in a hidden Chrome and prints the finished HTML as JSON.

Used by fetcher.py like this:  python -m app.render <url>
or, to click a button once and take the page it leads to:  python -m app.render <url> --click "<button text>"
"""
import json
import re
import sys

from playwright.sync_api import Error, sync_playwright

from .fetcher import USER_AGENT


def _settle(page) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Error:
        pass  # some pages never go fully quiet; what we have is good enough


def render(url: str, click: str | None = None) -> dict:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(user_agent=USER_AGENT, locale="de-DE")
            page = context.new_page()
            response = page.goto(url, wait_until="domcontentloaded", timeout=45000)
            _settle(page)
            if click:
                # The button or link with exactly this text (a "Zu den Stellen" button without an address)
                target = page.get_by_text(re.compile(r"^\s*" + re.escape(click) + r"\s*$", re.I)).first
                try:
                    with context.expect_page(timeout=8000) as opened:  # it may open a new tab ...
                        target.click(timeout=8000)
                    page = opened.value
                except Error:
                    pass  # ... or go on in the same tab (or do nothing)
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=15000)
                except Error:
                    pass
                _settle(page)
            return {
                "html": page.content(),
                "status": response.status if response else 0,
                "final_url": page.url,
            }
        finally:
            browser.close()


if __name__ == "__main__":
    click = sys.argv[sys.argv.index("--click") + 1] if "--click" in sys.argv else None
    result = render(sys.argv[1], click)
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdout.write(json.dumps(result))
