from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="截图并检查旧版核销 HTML。")
    parser.add_argument("source", help="HTML 文件路径或 http(s) 地址")
    parser.add_argument(
        "--screenshot",
        type=Path,
        default=Path(tempfile.gettempdir()) / "offline-audit-legacy-baseline.png",
    )
    args = parser.parse_args()
    source = args.source
    if not source.startswith(("http://", "https://", "file://")):
        source = Path(source).resolve().as_uri()
    mobile_screenshot = args.screenshot.with_name(
        args.screenshot.stem + "-mobile" + args.screenshot.suffix
    )

    console_errors: list[str] = []
    page_errors: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 960})
        page.on(
            "console",
            lambda message: console_errors.append(message.text)
            if message.type == "error"
            else None,
        )
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        response = page.goto(source, wait_until="networkidle")
        page.screenshot(path=str(args.screenshot), full_page=True)
        desktop = {
            "title": page.title(),
            "body_class": page.locator("body").get_attribute("class"),
            "h1": page.locator("h1").all_text_contents(),
            "buttons": page.get_by_role("button").all_text_contents()[:20],
            "body_text_prefix": page.locator("body").inner_text()[:500],
        }
        page.set_viewport_size({"width": 390, "height": 844})
        overflow = page.evaluate(
            "document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        page.screenshot(path=str(mobile_screenshot), full_page=True)
        browser.close()

    assert not console_errors, console_errors
    assert not page_errors, page_errors
    print(
        json.dumps(
            {
                "status": response.status if response else None,
                "source": source,
                "desktop": desktop,
                "mobile_overflow": overflow,
                "screenshot": str(args.screenshot),
                "mobile_screenshot": str(mobile_screenshot),
                "console_errors": console_errors,
                "page_errors": page_errors,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
