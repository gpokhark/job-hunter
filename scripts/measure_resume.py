#!/usr/bin/env python3
"""Render an HTML resume (or cover letter) in headless Chromium and report whether it fits the
target US Letter page count (0.5" margins on all sides); optionally save the PDF.

Usage:
    uv run python scripts/measure_resume.py <file.html> [--target-pages 1|1.5|2] [--save-pdf OUT.pdf]

Prints one JSON object (status ok|underflow|overflow, pages, last_page_fill_pct, guidance, ...) that
the resume-generator / outreach-writer skills read to decide whether to trim, expand or accept.
Needs the base dependencies (`playwright`, `pypdf`) plus a one-time Chromium download:
    uv sync --all-extras && uv run playwright install chromium
Fill percentages are measured on the machine that renders the PDF — fonts differ between
operating systems, so re-measure per machine rather than assuming a result is portable.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import tempfile
from pathlib import Path

from job_hunter.rootutil import add_project_argument, chdir_to_project_root

# US Letter at 96 dpi with 0.5in margins: content 960 px tall, 720 px wide.
PAGE_HEIGHT_PX = 960
LINE_HEIGHT_PX = 18   # ~10.5pt body at 1.2-1.25 line-height in Chromium
GOOD_FILL_MIN = 88    # % — below this a full-page target shows visible white space at the bottom
# For a half-page target (1.5 pages) the last page is deliberately only partly filled.
HALF_PAGE_FILL_BAND = (35, 70)

INSTALL_HELP = (
    "PDF rendering needs the project dependencies and a Chromium browser. Install them with:\n"
    "    uv sync --all-extras\n"
    "    uv run playwright install chromium\n"
    "(on a bare Linux host add system libraries with: uv run playwright install --with-deps chromium)"
)


class MissingDependency(RuntimeError):
    pass


def analyze(content_height: int, page_count: int, target_pages: float) -> dict:
    """Pure fill/status logic (no browser): how the rendered document compares with the target."""
    expected_pages = math.ceil(target_pages)
    is_half_target = (target_pages * 2) % 2 == 1
    fill_pct = round(content_height / PAGE_HEIGHT_PX * 100, 1)  # legacy: raw height over one page
    last_page_content_px = content_height - (page_count - 1) * PAGE_HEIGHT_PX
    last_page_fill_pct = round(max(last_page_content_px, 0) / PAGE_HEIGHT_PX * 100, 1)
    delta_px = content_height - expected_pages * PAGE_HEIGHT_PX
    delta_lines = round(abs(delta_px) / LINE_HEIGHT_PX)

    if page_count > expected_pages:
        status = "overflow"
        guidance = (
            f"OVERFLOW: rendered {page_count} pages but target is {target_pages} "
            f"(expected {expected_pages}). Content is {abs(delta_px)}px too tall "
            f"(~{delta_lines} lines over). Trim bullets or shorten existing ones to fit."
        )
    elif page_count < expected_pages:
        status = "underflow"
        guidance = (
            f"UNDERFLOW: rendered {page_count} page(s) but target is {target_pages} "
            f"(expected {expected_pages}). Content is {abs(delta_px)}px short "
            f"(~{delta_lines} lines) of even reaching the target page count. Expand bullets or add content."
        )
    else:
        band_low, band_high = HALF_PAGE_FILL_BAND if is_half_target else (GOOD_FILL_MIN, 100)
        if last_page_fill_pct < band_low:
            status = "underflow"
            short_px = round(band_low / 100 * PAGE_HEIGHT_PX - last_page_content_px)
            guidance = (
                f"UNDERFLOW: {page_count} page(s), last page only {last_page_fill_pct}% full "
                f"(target band {band_low}-{band_high}% for a {target_pages}-page resume). "
                f"Add ~{round(short_px / LINE_HEIGHT_PX)} lines to the last page."
            )
        elif last_page_fill_pct > band_high:
            status = "overflow"
            over_px = round(last_page_content_px - band_high / 100 * PAGE_HEIGHT_PX)
            guidance = (
                f"OVERFLOW: {page_count} page(s), last page {last_page_fill_pct}% full, over the "
                f"{band_high}% band for a {target_pages}-page resume by ~{round(over_px / LINE_HEIGHT_PX)} lines. Trim slightly."
            )
        else:
            status = "ok"
            guidance = (
                f"OK: {page_count} page(s) matching the {target_pages}-page target, "
                f"last page {last_page_fill_pct}% full (good fill, no changes needed)."
            )
    return {
        "status": status, "pages": page_count, "target_pages": target_pages,
        "last_page_fill_pct": last_page_fill_pct, "fill_pct": fill_pct,
        "content_height_px": content_height, "page_height_px": PAGE_HEIGHT_PX,
        "delta_px": delta_px, "delta_lines": delta_lines, "guidance": guidance,
    }


def measure(html_path: Path, pdf_output: Path | None = None, target_pages: float = 1.0) -> dict:
    try:
        from playwright.sync_api import sync_playwright
        from pypdf import PdfReader
    except ImportError as exc:
        raise MissingDependency(INSTALL_HELP) from exc

    delete_pdf = pdf_output is None
    pdf_path = str(pdf_output) if pdf_output else tempfile.mktemp(suffix=".pdf")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            # Height=1 makes scrollHeight the true content height; width 816 = 8.5in at 96 dpi.
            page = browser.new_page(viewport={"width": 816, "height": 1})
            page.goto(html_path.resolve().as_uri(), wait_until="networkidle")
            content_height = page.evaluate("document.documentElement.scrollHeight")
            page.pdf(
                path=pdf_path, format="Letter",
                margin={"top": "0.5in", "bottom": "0.5in", "left": "0.5in", "right": "0.5in"},
                print_background=True,
            )
            browser.close()
    except Exception as exc:  # noqa: BLE001 - Playwright raises its own Error type
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
            raise MissingDependency(INSTALL_HELP) from exc
        raise
    page_count = len(PdfReader(pdf_path).pages)
    if delete_pdf:
        Path(pdf_path).unlink(missing_ok=True)
    return analyze(int(content_height), page_count, target_pages)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("html", help="path to the HTML file")
    parser.add_argument("--save-pdf", metavar="PDF", help="also save the rendered PDF here")
    parser.add_argument("--target-pages", type=float, default=1.0, help="intended page count: 1, 1.5 or 2 (default 1)")
    add_project_argument(parser)
    args = parser.parse_args()
    chdir_to_project_root(args.project)
    html_path = Path(args.html)
    if not html_path.exists():
        print(json.dumps({"error": f"File not found: {html_path}"}))
        return 1
    try:
        result = measure(html_path, Path(args.save_pdf) if args.save_pdf else None, args.target_pages)
    except MissingDependency as exc:
        print(f"job-hunter: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
