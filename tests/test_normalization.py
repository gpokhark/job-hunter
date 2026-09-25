from datetime import UTC, datetime

from job_hunter.normalizer import (
    canonical_url,
    description_hash,
    extract_job_posting_ld,
    fallback_job_id,
    parse_display_date,
    parse_flexible_date,
    parse_relative_posted,
)


def test_hash_normalizes_whitespace():
    assert description_hash("hello\n world") == description_hash(" hello world ")


def test_canonical_url_removes_query_fragment_and_trailing_slash():
    assert canonical_url("HTTPS://EXAMPLE.COM/job/1/?x=2#top") == "https://example.com/job/1"


def test_fallback_id_is_stable():
    one = fallback_job_id("Acme", "Engineer", "Detroit, MI", "https://example.com/1")
    two = fallback_job_id(" acme ", " engineer ", "detroit, mi", "https://example.com/1?ref=x")
    assert one == two and len(one) == 64


def test_parse_relative_posted():
    now = datetime(2026, 8, 29, tzinfo=UTC)
    assert parse_relative_posted("Posted Today", now=now) == now
    assert parse_relative_posted("Posted Yesterday", now=now).day == 28
    assert parse_relative_posted("Posted 3 Days Ago", now=now).day == 26
    assert parse_relative_posted("Posted 30+ Days Ago", now=now) == datetime(2026, 7, 29, tzinfo=UTC)
    assert parse_relative_posted("Remote", now=now) is None
    assert parse_relative_posted(None) is None


def test_parse_display_date():
    assert parse_display_date("Aug 10, 2026") == datetime(2026, 8, 10, tzinfo=UTC)
    assert parse_display_date("Aug 4, 2026") == datetime(2026, 8, 4, tzinfo=UTC)
    assert parse_display_date("  Aug 10, 2026  \n") == datetime(2026, 8, 10, tzinfo=UTC)
    assert parse_display_date("not a date") is None
    assert parse_display_date(None) is None


def test_parse_display_date_british_sept_abbreviation():
    """Jaguar Land Rover's SuccessFactors RMK tenant spells September's abbreviation
    "Sept" (4 letters) instead of the standard 3-letter "Sep" every other month uses —
    confirmed live, e.g. "7 Sept 2026" — while every other month still parses normally."""
    assert parse_display_date("7 Sept 2026") == datetime(2026, 9, 7, tzinfo=UTC)
    assert parse_display_date("30 Aug 2026") == datetime(2026, 8, 30, tzinfo=UTC)


def test_parse_flexible_date_epoch_millis_and_seconds():
    assert parse_flexible_date("1762389866472").year == 2025
    assert parse_flexible_date("1762389866").year == 2025
    assert parse_flexible_date("2026-08-20T12:00:00Z").year == 2026
    assert parse_flexible_date(None) is None
    assert parse_flexible_date("not a date") is None


def test_parse_flexible_date_non_zero_padded():
    """The Toro Company's TalentBrew site emits its JSON-LD datePosted as "2026-8-19"
    (non-zero-padded month/day) — confirmed live, and fromisoformat rejects it outright."""
    assert parse_flexible_date("2026-8-19") == datetime(2026, 8, 19, tzinfo=UTC)
    assert parse_flexible_date("2026-08-09") == datetime(2026, 8, 9, tzinfo=UTC)


def test_job_posting_ld_tolerates_raw_newlines_inside_strings():
    """Brose pastes multi-line text into JSON-LD strings unescaped; strict json.loads rejects
    it ("Invalid control character"), which used to drop the whole block silently."""
    html = (
        '<script type="application/ld+json">{"@type": "JobPosting", '
        '"datePosted": "09-25-2026", "description": "line one\nline two"}</script>'
    )
    posting = extract_job_posting_ld(html)
    assert posting is not None and posting["datePosted"] == "09-25-2026"
    assert "line two" in posting["description"]


def test_job_posting_ld_still_skips_genuinely_broken_json():
    assert extract_job_posting_ld('<script type="application/ld+json">{not json</script>') is None


def test_display_date_accepts_day_month_abbrev_year_with_a_label_prefix():
    """Harman's Avature cards: "Date Posted: 24-Sep-2026" in a single span."""
    assert parse_display_date("Date Posted: 24-Sep-2026") == datetime(2026, 9, 24, tzinfo=UTC)
    assert parse_display_date("24-Sep-2026") == datetime(2026, 9, 24, tzinfo=UTC)
    assert parse_display_date("Posted: Aug 10, 2026") == datetime(2026, 8, 10, tzinfo=UTC)


def test_display_date_label_strip_does_not_touch_times_or_garbage():
    assert parse_display_date("Posted recently") is None
    assert parse_display_date("Date Posted: not a date") is None
