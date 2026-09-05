#!/usr/bin/env python3
"""Browser-level audit for editable controls and actionable UI semantics."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright
from ui_layout_audit import build_inline_html, capture_backend

USER_VIEWS = ["workspace", "tasks", "deliverables", "settings"]
JUDGE_PANELS = ["panorama", "dag", "scheduler", "collaboration", "communication", "memory", "verifier", "recovery", "lab", "lineage"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8080")
    ap.add_argument("--output-json", default="evidence/ui_interaction_audit_v1.9.0.json")
    args = ap.parse_args()

    result = {
        "base_url": args.base_url,
        "views": {},
        "editable_failures": [],
        "button_semantic_failures": [],
        "clickable_non_native_failures": [],
        "safe_click_failures": [],
    }
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, executable_path="/usr/bin/chromium", args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1366, "height": 768})
        page.set_default_timeout(5_000)
        payloads = capture_backend(args.base_url)
        html = build_inline_html(payloads)
        page.set_content(html, wait_until="load")
        page.wait_for_timeout(800)
        page.wait_for_function("() => document.querySelector('#sidebarStatus')?.textContent?.length > 0")

        # Static and dynamic action controls must use native button/a semantics.
        bad_buttons = page.eval_on_selector_all(
            "button",
            "els => els.filter(x => !x.getAttribute('type')).map(x => ({id:x.id,text:x.textContent.trim().slice(0,80)}))",
        )
        result["button_semantic_failures"].extend(bad_buttons)

        # Non-native click targets are allowed only for SVG graph marks when they expose button role + tab focus.
        clickable = page.eval_on_selector_all(
            "[data-node], [data-edge], [data-view], [data-go], [data-run], [data-start-scenario], [data-start-fault], [data-start-bench], [data-edit-agent], [data-delete-agent], [data-preview-artifact], [data-edit-endpoint], [data-probe-endpoint], [data-accept-endpoint], [data-delete-endpoint]",
            """els => els.map(x => ({tag:x.tagName.toLowerCase(), id:x.id||'', cls:x.className?.baseVal||x.className||'', role:x.getAttribute('role'), tabindex:x.getAttribute('tabindex'), dataNode:x.dataset.node||'', dataEdge:x.dataset.edge||''}))""",
        )
        for item in clickable:
            if item["tag"] in {"button", "a"}:
                continue
            if item["tag"] in {"g", "line", "path"} and (item["dataNode"] or item["dataEdge"]):
                if item["role"] == "button" and item["tabindex"] == "0":
                    continue
            result["clickable_non_native_failures"].append(item)

        for view in USER_VIEWS:
            nav = page.locator(f'[data-view="{view}"]:visible').first
            nav.click(timeout=5_000)
            page.wait_for_timeout(80)
            root = page.locator(f"#view-{view}")
            fields = root.locator("input, textarea, select")
            field_rows = []
            for i in range(fields.count()):
                el = fields.nth(i)
                if not el.is_visible():
                    continue
                meta = el.evaluate("e => ({tag:e.tagName.toLowerCase(),id:e.id,type:e.type||'',disabled:e.disabled,readOnly:e.readOnly,value:e.value})")
                meta["tested"] = False
                meta["editable"] = None
                if meta["disabled"] or meta["readOnly"]:
                    meta["editable"] = False
                    # Disabled provider key is intentional only when selected provider does not require a key.
                    if meta["id"] != "providerKey":
                        result["editable_failures"].append({"view": view, **meta, "reason": "unexpected_disabled_or_readonly"})
                else:
                    try:
                        original = meta["value"]
                        if meta["tag"] == "select":
                            opts = el.locator("option")
                            if opts.count() > 1:
                                target = opts.nth(opts.count() - 1).get_attribute("value")
                                el.select_option(target)
                                changed = el.input_value() == (target or "")
                                el.select_option(original)
                            else:
                                changed = True
                        elif meta["type"] in {"number"}:
                            el.fill("2")
                            changed = el.input_value() == "2"
                            el.fill(original)
                        elif meta["type"] in {"checkbox", "radio"}:
                            before = el.is_checked()
                            el.click()
                            changed = el.is_checked() != before
                            el.click()
                        else:
                            marker = "UI-AUDIT-可输入"
                            el.fill(marker)
                            changed = el.input_value() == marker
                            el.fill(original)
                        meta["tested"] = True
                        meta["editable"] = bool(changed)
                        if not changed:
                            result["editable_failures"].append({"view": view, **meta, "reason": "value_did_not_change"})
                    except Exception as exc:  # browser-level evidence, not a unit test
                        meta["tested"] = True
                        meta["editable"] = False
                        result["editable_failures"].append({"view": view, **meta, "reason": f"{type(exc).__name__}: {exc}"})
                field_rows.append(meta)

            buttons = root.locator("button:visible, a:visible")
            action_rows = []
            for i in range(buttons.count()):
                b = buttons.nth(i)
                action_rows.append(b.evaluate("e => ({tag:e.tagName.toLowerCase(),id:e.id||'',text:e.textContent.trim().replace(/\\s+/g,' ').slice(0,100),disabled:Boolean(e.disabled),href:e.getAttribute('href')||'',dataGo:e.dataset.go||''})"))
            result["views"][view] = {"fields": field_rows, "actions": action_rows}

        # Judge is deliberately separate from the ordinary user navigation.
        page.locator('[data-view="judge"]:visible').first.click(timeout=5_000)
        judge_rows = []
        for panel in JUDGE_PANELS:
            tab = page.locator(f'.deep-tab[data-deep="{panel}"]:visible')
            tab.click(timeout=5_000)
            page.wait_for_timeout(50)
            active = page.locator(f'[data-deep-panel="{panel}"]')
            judge_rows.append({"panel": panel, "visible": active.is_visible()})
            if not active.is_visible():
                result["safe_click_failures"].append({"name": f"judge_panel_{panel}", "reason": "panel_not_visible_after_click"})
        result["judge_panels"] = judge_rows

        # Advanced Agent Studio is reachable only through an explicit Settings action,
        # not as a normal sidebar destination. Test that transition without treating it
        # as a user-primary view.
        page.locator('[data-view="settings"]:visible').first.click(timeout=5_000)
        advanced = page.locator('[data-go="agents"]:visible')
        if advanced.count():
            advanced.first.click(timeout=5_000)
            if not page.locator('#view-agents').is_visible():
                result["safe_click_failures"].append({"name": "advanced_agent_studio_entry", "reason": "agent_studio_not_visible"})

        # Safe client-side behavior checks (no provider call / execution launch / delete).
        checks = []
        def safe(name, fn):
            try:
                ok = bool(fn())
                checks.append({"name": name, "passed": ok})
                if not ok:
                    result["safe_click_failures"].append({"name": name, "reason": "postcondition_false"})
            except Exception as exc:
                checks.append({"name": name, "passed": False})
                result["safe_click_failures"].append({"name": name, "reason": f"{type(exc).__name__}: {exc}"})

        safe("pause_refresh_toggle", lambda: (page.locator("#pauseBtn").click() or True) and page.locator("#pauseBtn").inner_text().strip() == "恢复刷新")
        page.locator("#pauseBtn").click()
        safe("judge_mode_navigation", lambda: (page.locator('[data-view="judge"]:visible').first.click() or True) and page.locator("#view-judge").is_visible())
        page.locator('[data-view="settings"]:visible').first.click()
        page.locator('[data-go="agents"]:visible').first.click()
        page.locator("#agentId").fill("temporary-value")
        safe("new_agent_clears_form", lambda: (page.locator("#newAgentBtn").click() or True) and page.locator("#agentId").input_value() == "")
        page.locator('[data-view="settings"]').click()
        page.locator("#endpointName").fill("temporary-value")
        safe("new_endpoint_clears_form", lambda: (page.locator("#newEndpointBtn").click() or True) and page.locator("#endpointName").input_value() == "")
        result["safe_click_checks"] = checks

        browser.close()

    failure_count = sum(len(result[k]) for k in ("editable_failures", "button_semantic_failures", "clickable_non_native_failures", "safe_click_failures"))
    result["passed"] = failure_count == 0
    result["failure_count"] = failure_count
    output = Path(args.output_json)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": result["passed"], "failure_count": failure_count, "output": str(output)}, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
