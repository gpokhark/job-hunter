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


_COMMENT_OPEN = "<!--"
_COMMENT_CLOSE = "-->"
_SCRIPT_OPEN = re.compile(r"<(script|style)\b", re.I)
_CLOSERS = {
    "script": re.compile(r"</script\s*>", re.I),
    "style": re.compile(r"</style\s*>", re.I),
}
_HEADER_WS = re.compile(r"[\s\x00-\x1f\x7f\u2028\u2029]+")
_BREAK = re.compile(r"<br\b[^>]*>", re.I)
_LI_START = re.compile(r"<li\b[^>]*>", re.I)
_BLOCK_END = re.compile(r"</(?:p|div|h[1-6]|ul|ol|table|tr|section|article|blockquote)\s*>", re.I)
_TAG = re.compile(r"</?[A-Za-z][^<>]*>")
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")
_ISO_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_MAX_PART = 60


def _strip_comments(value: str) -> str:
    """Drop HTML comments in linear time; an unterminated one is dropped to the end of the input."""
    out: list[str] = []
    pos = 0
    while True:
        start = value.find(_COMMENT_OPEN, pos)
        if start == -1:
            out.append(value[pos:])
            break
        out.append(value[pos:start])
        end = value.find(_COMMENT_CLOSE, start + len(_COMMENT_OPEN))
        if end == -1:
            break
        pos = end + len(_COMMENT_CLOSE)
    return "".join(out)


def _strip_script_style(value: str) -> str:
    """Drop <script>/<style> elements in linear time; an unterminated one is dropped to the end of
    the input (its body must never leak into the JD)."""
    out: list[str] = []
    pos = 0
    while True:
        opener = _SCRIPT_OPEN.search(value, pos)
        if opener is None:
            out.append(value[pos:])
            break
        out.append(value[pos:opener.start()])
        closer = _CLOSERS[opener.group(1).lower()].search(value, opener.end())
        if closer is None:
            break
        pos = closer.end()
    return "".join(out)


def _header(value: object, fallback: str = "Not specified") -> str:
    """One physical line: whitespace/control characters (newlines, U+2028/2029...) collapse to a
    single space so job text can't forge a section line."""
    return _HEADER_WS.sub(" ", str(value or "")).strip() or fallback


def html_to_text(value: str) -> str:
    """Readable plain text from an HTML (or already-plain) job description: paragraph/list/line
    breaks are kept, bullets become '- ', entities are decoded, scripts/styles are dropped. A bare
    '<' that isn't a tag (e.g. 'salary < 100k') is left alone."""
    text = _strip_script_style(_strip_comments(value))
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
    header = [_header(job.get("title")), _header(job.get("location_raw"))]
    if _header(job.get("department"), ""):
        header.append(_header(job["department"]))
    parts = [
        "\n".join(header),
        "Summary\n"
        f"Posted: {posted}\n"
        f"Job ID: {_header(job['job_id'])}\n"
        f"Job URL: {_header(job.get('url'))}\n"
        f"Source: {_header(job['company'])} ({_header(job['source_key'])})",
        f"Description\n{description_text}",
    ]
    if job.get("salary_evidence"):
        parts.append(f"Pay & Benefits\n{_header(job['salary_evidence'])}")
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
