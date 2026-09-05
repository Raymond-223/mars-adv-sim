#!/usr/bin/env python3
"""Render the current product UI at competition viewport sizes using live backend DTOs.

Chromium may be policy-blocked from navigating to loopback in some build sandboxes.
This audit therefore fetches DTOs from the live local console with Python, then renders
THE CURRENT index.html/style.css/app.js fully inline. The browser receives a tiny fetch
shim that serves only those captured DTOs. No synthetic task metrics are invented.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "apps" / "console" / "frontend"
USER_VIEWS = ["workspace", "tasks", "deliverables", "settings"]
JUDGE_PANELS = ["panorama", "dag", "scheduler", "collaboration", "communication", "memory", "verifier", "recovery", "lab", "lineage"]
VIEWPORTS = [(1280, 720), (1366, 768), (1920, 1080)]


def get_json(base_url: str, path: str) -> Any:
    req = urllib.request.Request(base_url.rstrip("/") + path, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


def capture_backend(base_url: str) -> dict[str, Any]:
    runs = get_json(base_url, "/api/runs")
    run_id = runs[0]["run_id"] if runs else None
    snapshot_path = "/api/snapshot" + ("?run_id=" + urllib.parse.quote(run_id) if run_id else "")
    return {
        "/api/runs": runs,
        "/api/control/status": get_json(base_url, "/api/control/status"),
        "/api/settings/providers": get_json(base_url, "/api/settings/providers"),
        "/api/settings/endpoints": get_json(base_url, "/api/settings/endpoints"),
        "/api/settings/agents": get_json(base_url, "/api/settings/agents"),
        "/api/snapshot": get_json(base_url, snapshot_path),
        "/api/control/artifacts": get_json(base_url, "/api/control/artifacts"),
    }


def build_inline_html(payloads: dict[str, Any]) -> str:
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    css = (FRONTEND / "assets" / "style.css").read_text(encoding="utf-8")
    js = (FRONTEND / "assets" / "app.js").read_text(encoding="utf-8")
    html = re.sub(r'<link\s+rel="stylesheet"\s+href="assets/style\.css"\s*/?>', f"<style>{css}</style>", html)
    html = re.sub(r'<script\s+src="assets/app\.js"\s*></script>', "", html)
    data = json.dumps(payloads, ensure_ascii=False).replace("</", "<\\/")
    prelude = f"""
<script>
const __MOSAIC_AUDIT_PAYLOADS__ = {data};
window.fetch = async function(url, options={{}}) {{
  const raw = String(url || '');
  const parsed = new URL(raw, 'https://mosaic.audit.invalid/');
  const path = parsed.pathname;
  let key = path;
  if (path === '/api/snapshot') key = '/api/snapshot';
  const value = __MOSAIC_AUDIT_PAYLOADS__[key];
  if (value === undefined) {{
    return new Response(JSON.stringify({{error:'audit fixture has no captured response for '+path}}), {{status:404, headers:{{'Content-Type':'application/json'}}}});
  }}
  return new Response(JSON.stringify(value), {{status:200, headers:{{'Content-Type':'application/json'}}}});
}};
</script>
<script>{js}</script>
"""
    return html.replace("</body>", prelude + "</body>")


async def run_audit(html: str, out_dir: Path) -> dict[str, Any]:
    from playwright.async_api import async_playwright

    out_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, Any] = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, executable_path="/usr/bin/chromium", args=["--no-sandbox"])
        for width, height in VIEWPORTS:
            page = await browser.new_page(viewport={"width": width, "height": height})
            runtime_errors: list[str] = []
            page.on("pageerror", lambda exc, bucket=runtime_errors: bucket.append(str(exc)))
            await page.set_content(html, wait_until="load", timeout=30_000)
            await page.wait_for_timeout(700)
            vp_key = f"{width}x{height}"
            results[vp_key] = {}

            # Ordinary users see only the four product views. Hidden compatibility
            # routes are intentionally not clicked here.
            for view in USER_VIEWS:
                await page.locator(f'[data-view="{view}"]:visible').first.click(timeout=5_000)
                await page.wait_for_timeout(120)
                metrics = await page.evaluate(
                    """(surface) => ({
                      clientWidth: document.documentElement.clientWidth,
                      scrollWidth: document.documentElement.scrollWidth,
                      bodyScrollWidth: document.body.scrollWidth,
                      active: document.querySelector('.view.active')?.id || null,
                      deep: document.querySelector('.deep-panel.active')?.dataset.deepPanel || null,
                      h1: document.querySelector('h1#pageTitle')?.textContent?.trim() || null,
                      text: (document.querySelector('#view-'+surface)?.innerText || '').slice(0, 1800),
                      horizontalOverflowPx: Math.max(0, document.documentElement.scrollWidth - document.documentElement.clientWidth)
                    })""",
                    view,
                )
                metrics["runtimeErrors"] = list(runtime_errors)
                results[vp_key][f"user_{view}"] = metrics
                if (width, height) == (1366, 768):
                    await page.screenshot(path=str(out_dir / f"user_{view}_{width}x{height}.png"), full_page=False)

            # Judge mode is one explicit area with ten deep-dive panels.
            await page.locator('[data-view="judge"]:visible').first.click(timeout=5_000)
            await page.wait_for_timeout(150)
            for panel in JUDGE_PANELS:
                await page.locator(f'.deep-tab[data-deep="{panel}"]:visible').click(timeout=5_000)
                await page.wait_for_timeout(120)
                metrics = await page.evaluate(
                    """(panel) => ({
                      clientWidth: document.documentElement.clientWidth,
                      scrollWidth: document.documentElement.scrollWidth,
                      bodyScrollWidth: document.body.scrollWidth,
                      active: document.querySelector('.view.active')?.id || null,
                      deep: document.querySelector('.deep-panel.active')?.dataset.deepPanel || null,
                      h1: document.querySelector('h1#pageTitle')?.textContent?.trim() || null,
                      text: (document.querySelector('[data-deep-panel="'+panel+'"]')?.innerText || '').slice(0, 1800),
                      horizontalOverflowPx: Math.max(0, document.documentElement.scrollWidth - document.documentElement.clientWidth)
                    })""",
                    panel,
                )
                metrics["runtimeErrors"] = list(runtime_errors)
                results[vp_key][f"judge_{panel}"] = metrics
                if (width, height) == (1366, 768):
                    await page.screenshot(path=str(out_dir / f"judge_{panel}_{width}x{height}.png"), full_page=False)
            await page.close()
        await browser.close()
    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8080")
    ap.add_argument("--output-dir", default="evidence/ui_screens_v1.9.0")
    ap.add_argument("--output-json", default="evidence/ui_layout_audit_v1.9.0.json")
    args = ap.parse_args()

    payloads = capture_backend(args.base_url)
    html = build_inline_html(payloads)
    results = asyncio.run(run_audit(html, PROJECT_ROOT / args.output_dir))
    overflows = [
        {"viewport": vp, "surface": surface, "px": data["horizontalOverflowPx"]}
        for vp, surfaces in results.items() for surface, data in surfaces.items() if data["horizontalOverflowPx"] > 0
    ]
    runtime_errors = [
        {"viewport": vp, "surface": surface, "errors": data.get("runtimeErrors", [])}
        for vp, surfaces in results.items() for surface, data in surfaces.items() if data.get("runtimeErrors")
    ]
    snap = payloads["/api/snapshot"]
    report = {
        "release": "MOSAIC-Omega-V3-XH202631-Product-First-v1.9.0",
        "generated_at": time.time(),
        "browser_method": "current HTML/CSS/JS rendered fully inline with DTOs captured from the actual local v1.9.0 console backend; no synthetic task metrics",
        "source_run_id": snap.get("run", {}).get("run_id"),
        "source_execution_verdict": snap.get("execution", {}).get("verdict"),
        "viewports": results,
        "surface_contract": {"user_views": USER_VIEWS, "judge_panels": JUDGE_PANELS, "surface_count_per_viewport": len(USER_VIEWS) + len(JUDGE_PANELS)},
        "horizontal_overflow_failures": overflows,
        "runtime_error_failures": runtime_errors,
        "passed": not overflows and not runtime_errors,
    }
    out_json = PROJECT_ROOT / args.output_json
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "passed": report["passed"],
        "source_run_id": report["source_run_id"],
        "source_execution_verdict": report["source_execution_verdict"],
        "viewports": list(results),
        "surfaces_per_viewport": len(USER_VIEWS) + len(JUDGE_PANELS),
        "total_surface_checks": len(VIEWPORTS) * (len(USER_VIEWS) + len(JUDGE_PANELS)),
        "overflow_failures": overflows,
        "runtime_error_failures": runtime_errors,
        "output": str(out_json),
    }, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 9


if __name__ == "__main__":
    raise SystemExit(main())
