"""Opens one page in a hidden Chrome and prints the finished HTML as JSON.

Used by fetcher.py like this:  python -m app.render <url>
"""
import json
import sys

from playwright.sync_api import Error, sync_playwright

from .fetcher import USER_AGENT


def render(url: str) -> dict:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=USER_AGENT, locale="de-DE")
            response = page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Error:
                pass  # some pages never go fully quiet; what we have is good enough
            return {
                "html": page.content(),
                "status": response.status if response else 0,
                "final_url": page.url,
            }
        finally:
            browser.close()


if __name__ == "__main__":
    result = render(sys.argv[1])
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdout.write(json.dumps(result))
