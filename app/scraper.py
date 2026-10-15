"""Playwright scraper for public e-tender product pages.

This scraper records HTTP/navigation diagnostics and does not bypass
authentication, CAPTCHA, rate limits, or access controls.
"""
from __future__ import annotations

import re
import asyncio
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

BASE_URL = "https://etender.gov.az/main/competition/products/{page_id}"
NAVIGATION_TIMEOUT_MS = 20_000
DOM_READY_TIMEOUT_MS = 8_000
BODY_TIMEOUT_MS = 8_000


def clean(value: str | None) -> str:
    return re.sub(r"\\s+", " ", value or "").strip()


def _http_probe(url: str) -> dict[str, Any]:
    """Independent standard-library HTTP check; does not evade access controls."""
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; eTenderAnalyzer/1.0)"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            sample = response.read(4096).decode("utf-8", errors="replace")
            return {
                "ok": True,
                "status": response.status,
                "final_url": response.geturl(),
                "content_type": response.headers.get("Content-Type"),
                "sample": clean(sample)[:500],
            }
    except urllib.error.HTTPError as exc:
        try:
            sample = exc.read(2048).decode("utf-8", errors="replace")
        except Exception:
            sample = ""
        return {
            "ok": False,
            "status": exc.code,
            "final_url": exc.geturl(),
            "error": f"HTTPError: {exc.code} {exc.reason}",
            "sample": clean(sample)[:300],
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
        }


async def scrape_page(page_id: int) -> dict[str, Any]:
    url = BASE_URL.format(page_id=page_id)
    result: dict[str, Any] = {
        "page_id": page_id,
        "source_url": url,
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "page_status": "unknown",
        "details": None,
        "http_status": None,
        "final_url": url,
        "page_title": None,
        "products": [],
    }

    # Compare a plain HTTP request with Chromium from the same Render instance.
    http_probe = await asyncio.to_thread(_http_probe, url)
    result["http_probe"] = http_probe

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/128.0.0.0 Safari/537.36"
                ),
                ignore_https_errors=False,
            )
            page = await context.new_page()
            page.set_default_timeout(BODY_TIMEOUT_MS)

            # "commit" waits for the response to begin, not for every portal
            # script/resource to finish loading. A failed navigation is recorded.
            response = None
            try:
                response = await page.goto(
                    url,
                    wait_until="commit",
                    timeout=NAVIGATION_TIMEOUT_MS,
                )
            except PlaywrightTimeoutError:
                result.update(
                    page_status="navigation_timeout",
                    details=(
                        f"Playwright received no navigation response/commit within "
                        f"{NAVIGATION_TIMEOUT_MS // 1000}s. "
                        f"Independent HTTP probe: {http_probe!r}"
                    ),
                )
                return result

            result["http_status"] = response.status if response else None
            result["final_url"] = page.url

            # HTTP/access errors should be reported before parsing the body.
            if response and response.status in (401, 403, 429):
                result.update(
                    page_status=f"http_{response.status}",
                    details="Portal returned an access or rate-limit response.",
                )
                return result
            if response and response.status >= 500:
                result.update(
                    page_status=f"http_{response.status}",
                    details="Portal returned a server error response.",
                )
                return result

            # DOMContentLoaded is helpful but not required: some portals keep
            # navigation events pending because of scripts or third-party assets.
            try:
                await page.wait_for_load_state(
                    "domcontentloaded", timeout=DOM_READY_TIMEOUT_MS
                )
            except PlaywrightTimeoutError:
                result["details"] = (
                    f"DOMContentLoaded not observed within "
                    f"{DOM_READY_TIMEOUT_MS // 1000}s; inspected current document."
                )

            try:
                await page.locator("body").wait_for(
                    state="attached", timeout=BODY_TIMEOUT_MS
                )
                body = clean(await page.locator("body").inner_text(timeout=BODY_TIMEOUT_MS))
                result["page_title"] = clean(await page.title())
            except PlaywrightTimeoutError:
                result.update(
                    page_status="body_timeout",
                    details="Response began, but readable page body was unavailable.",
                )
                return result

            low = body.lower()
            if any(
                term in low
                for term in ("captcha", "robot yoxlaması", "robot verification")
            ):
                result.update(
                    page_status="captcha",
                    details="CAPTCHA/robot check detected; manual review required.",
                )
                return result

            tables = page.locator("table")
            table_count = await tables.count()

            if any(term in low for term in ("daxil ol", "sistemə giriş", "login")) and not table_count:
                result.update(
                    page_status="login_required",
                    details="Login-like page detected; no bypass attempted.",
                )
                return result

            if not table_count:
                result.update(
                    page_status="table_not_found",
                    details=(
                        (result["details"] + " ") if result["details"] else ""
                    ) + f"No HTML table found. Title={result['page_title']!r}; "
                    f"HTTP={result['http_status']}; body={body[:700]!r}",
                )
                return result

            for ti in range(table_count):
                table = tables.nth(ti)
                rows = table.locator("tr")
                row_count = await rows.count()
                if not row_count:
                    continue

                headers = [
                    clean(value)
                    for value in await rows.nth(0).locator("th,td").all_inner_texts()
                ]

                for ri in range(1, row_count):
                    cells = [
                        clean(value)
                        for value in await rows.nth(ri).locator("th,td").all_inner_texts()
                    ]
                    if not cells or not any(cells):
                        continue

                    row: dict[str, Any] = {
                        "page_id": page_id,
                        "row_no": ri,
                        "product_name": None,
                        "description": None,
                        "quantity": None,
                        "unit": None,
                        "product_code": None,
                        "category": None,
                        "source_url": url,
                        "collected_at": result["collected_at"],
                        "page_status": "ok",
                    }

                    for idx, value in enumerate(cells):
                        heading = headers[idx].lower() if idx < len(headers) else ""
                        if any(term in heading for term in ("başlıq", "məhsul", "malın adı", "ad")):
                            row["product_name"] = value
                        elif any(term in heading for term in ("təsvir", "description", "spesifikasiya")):
                            row["description"] = value
                        elif "miqdar" in heading or "quantity" in heading:
                            row["quantity"] = value
                        elif "ölçü vahidi" in heading or "vahid" in heading or "unit" in heading:
                            row["unit"] = value
                        elif "kod" in heading or "code" in heading:
                            row["product_code"] = value
                        elif "kateqoriya" in heading or "category" in heading:
                            row["category"] = value

                    if not row["product_name"] and cells:
                        row["product_name"] = cells[0]
                    result["products"].append(row)

            result["page_status"] = "ok" if result["products"] else "empty_or_unrecognized"
            if not result["products"] and not result["details"]:
                result["details"] = (
                    f"{table_count} HTML table(s) found, but no product rows were "
                    "recognized. Check page structure and column headers."
                )

        except Exception as exc:
            result.update(page_status="error", details=f"{type(exc).__name__}: {str(exc)[:900]}")
        finally:
            await browser.close()

    return result
