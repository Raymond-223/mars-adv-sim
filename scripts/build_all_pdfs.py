# -*- coding: utf-8 -*-
"""Unified PDF builder for README, COMPREHENSIVE_REPORT, and BEGINNER_GUIDE."""

import os
import subprocess
from pathlib import Path
import markdown

WORKSPACE_ROOT = Path(r"C:/Users/pangzuyu/Downloads/mars-adv-sim")

DOCS_TO_BUILD = [
    ("README.md", "README.pdf", "MOSAIC-Ω V3 项目总说明与系统架构指南"),
    ("COMPREHENSIVE_REPORT.md", "COMPREHENSIVE_REPORT.pdf", "MOSAIC-Ω 协同推理综合技术报告与测试指南"),
    ("BEGINNER_GUIDE.md", "BEGINNER_GUIDE.pdf", "MOSAIC-Ω 通俗解读与深度技术原理指南"),
]

css_style = """
<style>
    @page {
        size: A4;
        margin: 20mm 15mm 20mm 15mm;
    }
    body {
        font-family: "Microsoft YaHei", "Segoe UI", "PingFang SC", sans-serif;
        font-size: 13px;
        line-height: 1.6;
        color: #24292e;
        padding: 0;
        margin: 0;
    }
    h1 {
        font-size: 22px;
        border-bottom: 2px solid #2b6cb0;
        padding-bottom: 8px;
        color: #1a202c;
        margin-top: 20px;
        margin-bottom: 15px;
        page-break-after: avoid;
    }
    h2 {
        font-size: 17px;
        border-bottom: 1px solid #e2e8f0;
        padding-bottom: 5px;
        color: #2b6cb0;
        margin-top: 18px;
        margin-bottom: 10px;
        page-break-after: avoid;
    }
    h3 {
        font-size: 14px;
        color: #2d3748;
        margin-top: 14px;
        margin-bottom: 8px;
        page-break-after: avoid;
    }
    table {
        width: 100%;
        border-collapse: collapse;
        margin: 12px 0;
        font-size: 12px;
        page-break-inside: avoid;
    }
    th, td {
        border: 1px solid #cbd5e0;
        padding: 7px 10px;
        text-align: left;
    }
    th {
        background-color: #ebf8ff;
        color: #2b6cb0;
        font-weight: bold;
    }
    tr:nth-child(even) {
        background-color: #f7fafc;
    }
    code {
        font-family: "Consolas", "Courier New", monospace;
        background-color: #edf2f7;
        padding: 2px 5px;
        border-radius: 4px;
        font-size: 11px;
        color: #c53030;
    }
    pre {
        background-color: #1a202c;
        color: #f7fafc;
        padding: 12px;
        border-radius: 6px;
        overflow-x: auto;
        font-size: 11px;
        line-height: 1.45;
        page-break-inside: avoid;
    }
    pre code {
        background-color: transparent;
        color: #f7fafc;
        padding: 0;
    }
    blockquote {
        border-left: 4px solid #3182ce;
        background-color: #ebf8ff;
        margin: 10px 0;
        padding: 8px 12px;
        color: #2c5282;
        border-radius: 0 4px 4px 0;
    }
    ul, ol {
        padding-left: 20px;
        margin: 8px 0;
    }
    li {
        margin-bottom: 4px;
    }
    hr {
        border: none;
        border-top: 1px solid #e2e8f0;
        margin: 20px 0;
    }
</style>
"""

edge_paths = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"
]

edge_exe = None
for p in edge_paths:
    if os.path.exists(p):
        edge_exe = p
        break

if not edge_exe:
    raise FileNotFoundError("Microsoft Edge executable not found for PDF printing.")

def build_pdf(md_name: str, pdf_name: str, title: str) -> None:
    md_file = WORKSPACE_ROOT / md_name
    html_file = WORKSPACE_ROOT / f"{md_file.stem}_temp.html"
    pdf_file = WORKSPACE_ROOT / pdf_name

    if not md_file.exists():
        print(f"Skipping missing file: {md_file}")
        return

    with md_file.open(encoding="utf-8") as f:
        md_text = f.read()

    html_body = markdown.markdown(md_text, extensions=["tables", "fenced_code", "toc", "nl2br"])
    full_html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>{title}</title>
    {css_style}
</head>
<body>
    {html_body}
</body>
</html>
"""
    with html_file.open("w", encoding="utf-8") as f:
        f.write(full_html)

    cmd = [
        edge_exe,
        "--headless",
        "--disable-gpu",
        f"--print-to-pdf={pdf_file}",
        "--no-pdf-header-footer",
        str(html_file)
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)

    if html_file.exists():
        html_file.unlink()

    if res.returncode == 0 and pdf_file.exists():
        print(f"[{pdf_name}] PDF generated successfully ({pdf_file.stat().st_size} bytes)")
    else:
        print(f"[{pdf_name}] Build failed: {res.stderr}")

def main():
    print("=== Building PDF Documentation ===")
    for md_name, pdf_name, title in DOCS_TO_BUILD:
        build_pdf(md_name, pdf_name, title)
    print("=== Build Completed ===")

if __name__ == "__main__":
    main()
