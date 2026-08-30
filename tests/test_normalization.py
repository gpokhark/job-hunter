from datetime import UTC, datetime

from job_hunter.normalizer import (
    canonical_url,
    description_hash,
    fallback_job_id,
    parse_display_date,
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
