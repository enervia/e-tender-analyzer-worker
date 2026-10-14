"""Playwright scraper for public e-tender product pages.

Does not bypass authentication, CAPTCHA, rate limits, or access controls.
Verify portal behavior with a small ID range before larger scans.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

BASE_URL = "https://etender.gov.az/main/competition/products/{page_id}"


def clean(value: str | None) -> str:
    return re.sub(r"\\s+", " ", value or "").strip()


async def scrape_page(page_id: int) -> dict[str, Any]:
    result: dict[str, Any] = {
        "page_id": page_id,
        "source_url": BASE_URL.format(page_id=page_id),
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "page_status": "unknown",
        "details": None,
        "products": [],
    }

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page()
            page.set_default_timeout(15000)

            # Prefer DOM readiness, but if the portal's scripts keep navigation
            # open too long, retry at the response-commit stage and inspect what
            # actually arrived. This does not bypass portal access controls.
            try:
                response = await page.goto(
                    result["source_url"],
                    wait_until="domcontentloaded",
                    timeout=45000,
                )
            except PlaywrightTimeoutError:
                try:
                    response = await page.goto(
                        result["source_url"],
                        wait_until="commit",
                        timeout=20000,
                    )
                    result["details"] = "DOM-ready navigation timed out; inspected response after commit."
                except PlaywrightTimeoutError:
                    result.update(
                        page_status="timeout",
                        details="Portal did not respond within 45s (DOM-ready) plus 20s (commit retry).",
                    )
                    return result

            try:
                await page.locator("body").wait_for(state="attached", timeout=15000)
                await page.wait_for_timeout(1200)
                body = clean(await page.locator("body").inner_text(timeout=15000))
            except PlaywrightTimeoutError:
                result.update(
                    page_status="timeout",
                    details="Navigation responded, but page body did not become readable within 15 seconds.",
                )
                return result

            low = body.lower()
            if response is not None and response.status in (401, 403, 429):
                result.update(
                    page_status=f"http_{response.status}",
                    details="Portal returned access or rate-limit status.",
                )
                return result
            if any(term in low for term in ("captcha", "robot yoxlaması", "robot verification")):
                result.update(
                    page_status="captcha",
                    details="CAPTCHA/robot check detected; manual review required.",
                )
                return result

            tables = page.locator("table")
            count = await tables.count()
            if any(term in low for term in ("daxil ol", "sistemə giriş", "login")) and not count:
                result.update(
                    page_status="login_required",
                    details="Login-like page detected; no bypass attempted.",
                )
                return result
            if not count:
                result.update(page_status="table_not_found", details=body[:1000] or "Page body was empty.")
                return result

            for ti in range(count):
                table = tables.nth(ti)
                rows = table.locator("tr")
                n = await rows.count()
                headers = (
                    [clean(x) for x in await rows.nth(0).locator("th,td").all_inner_texts()]
                    if n
                    else []
                )
                for ri in range(1, n):
                    cells = [clean(x) for x in await rows.nth(ri).locator("th,td").all_inner_texts()]
                    if not cells or not any(cells):
                        continue
                    row = {
                        "page_id": page_id,
                        "row_no": ri,
                        "product_name": None,
                        "description": None,
                        "quantity": None,
                        "unit": None,
                        "product_code": None,
                        "category": None,
                        "source_url": result["source_url"],
                        "collected_at": result["collected_at"],
                        "page_status": "ok",
                    }
                    for idx, val in enumerate(cells):
                        heading = headers[idx].lower() if idx < len(headers) else ""
                        if any(s in heading for s in ("başlıq", "məhsul", "malın adı", "ad")):
                            row["product_name"] = val
                        elif any(s in heading for s in ("təsvir", "description", "spesifikasiya")):
                            row["description"] = val
                        elif "miqdar" in heading or "quantity" in heading:
                            row["quantity"] = val
                        elif "ölçü vahidi" in heading or "vahid" in heading or "unit" in heading:
                            row["unit"] = val
                        elif "kod" in heading or "code" in heading:
                            row["product_code"] = val
                        elif "kateqoriya" in heading or "category" in heading:
                            row["category"] = val
                    if not row["product_name"] and cells:
                        row["product_name"] = cells[0]
                    result["products"].append(row)

            result["page_status"] = "ok" if result["products"] else "empty_or_unrecognized"
            if not result["products"] and not result["details"]:
                result["details"] = "Table rendered but expected product rows were not recognized."
        except PlaywrightTimeoutError as exc:
            result.update(page_status="timeout", details=f"Playwright timeout: {str(exc)[:700]}")
        except Exception as exc:
            result.update(page_status="error", details=str(exc)[:1000])
        finally:
            await browser.close()

    return result
