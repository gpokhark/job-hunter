"""Export one collected job's description as the plain-text JD file the resume/outreach skills
consume (spec section 6.3). Python owns this retrieval; the skills never read SQLite.

The file is a deliberate subset of the format the standalone scraper used: header fields plus the
whole description as plain text. Sections that need an LLM to split (Minimum/Preferred
Qualifications) are *not* fabricated here; the skills read the full description.
"""

from __future__ import annotations

import html
import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .atomic import atomic_write_text
from .storage import Storage


class JobNotFound(LookupError):
    pass


class JobHasNoDescription(ValueError):
    pass


@dataclass(frozen=True)
class ExportResult:
    path: Path
    relative_path: str
    created: bool
    prompt: str


_SCRIPT_STYLE = re.compile(r"<(script|style)\b.*?</\1\s*>", re.I | re.S)
_BREAK = re.compile(r"<br\b[^>]*>", re.I)
_LI_START = re.compile(r"<li\b[^>]*>", re.I)
_BLOCK_END = re.compile(r"</(?:p|div|h[1-6]|ul|ol|table|tr|section|article|blockquote)\s*>", re.I)
_TAG = re.compile(r"</?[A-Za-z][^<>]*>")
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")
_ISO_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_MAX_PART = 60


def html_to_text(value: str) -> str:
    """Readable plain text from an HTML (or already-plain) job description: paragraph/list/line
    breaks are kept, bullets become '- ', entities are decoded, scripts/styles are dropped. A bare
    '<' that isn't a tag (e.g. 'salary < 100k') is left alone."""
    text = _SCRIPT_STYLE.sub("", value)
    text = _BREAK.sub("\n", text)
    text = _LI_START.sub("\n- ", text)
    text = _BLOCK_END.sub("\n\n", text)
    text = _TAG.sub("", text)
    text = html.unescape(text).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def sanitize(text: str | None, fallback: str) -> str:
    """A filename component: whitespace -> '_', everything outside [A-Za-z0-9_-] dropped, runs of
    '_' collapsed, edge '_'/'-' trimmed, length-capped. Never contains a path separator; `fallback` if nothing is left."""
    cleaned = re.sub(r"_+", "_", _UNSAFE.sub("", re.sub(r"\s+", "_", (text or "").strip())))
    return cleaned[:_MAX_PART].strip("_-") or fallback


def _first_words(title: str | None, count: int = 4) -> str:
    return " ".join((title or "").split()[:count])


def render_jd_text(job: dict, description_text: str) -> str:
    posted = (job.get("posted_at") or "")[:10]
    posted = posted if _ISO_DAY.fullmatch(posted) else "Not specified"
    header = [job.get("title") or "Not specified", job.get("location_raw") or "Not specified"]
    if job.get("department"):
        header.append(job["department"])
    parts = [
        "\n".join(header),
        "Summary\n"
        f"Posted: {posted}\n"
        f"Job ID: {job['job_id']}\n"
        f"Job URL: {job.get('url') or 'Not specified'}\n"
        f"Source: {job['company']} ({job['source_key']})",
        f"Description\n{description_text}",
    ]
    if job.get("salary_evidence"):
        parts.append(f"Pay & Benefits\n{job['salary_evidence']}")
    return "\n\n".join(parts) + "\n"


def export_jd(
    storage: Storage, source_key: str, job_id: str, *, project_root: Path, output_root: Path,
    today: date,
) -> ExportResult:
    """Write (or reuse) `<output_root>/<Company>/JD_<Company>_<Title>_<YYYY-MM-DD>[_N].txt`.

    Identical content the same day reuses the existing file; a *changed* description gets the next
    numbered file so the JD an earlier tailored CV was written against is never overwritten."""
    job = storage.get_job_for_jd(source_key, job_id)
    if job is None:
        raise JobNotFound(f"job {source_key}/{job_id} not found")
    body = html_to_text(job.get("description") or "")
    if not body:
        raise JobHasNoDescription(f"job {source_key}/{job_id} has no description text")
    text = render_jd_text(job, body)
    company = sanitize(job["company"], "Company")
    base = f"JD_{company}_{sanitize(_first_words(job['title']), 'Job')}_{today.isoformat()}"
    directory = Path(output_root) / company
    number = 1
    while True:
        path = directory / (f"{base}.txt" if number == 1 else f"{base}_{number}.txt")
        if not path.exists():
            atomic_write_text(path, text)
            created = True
            break
        if path.read_text(encoding="utf-8") == text:
            created = False
            break
        number += 1
    relative = Path(os.path.relpath(path.resolve(), Path(project_root).resolve())).as_posix()
    return ExportResult(path.resolve(), relative, created, f"Use the resume-generator skill on {relative}")
