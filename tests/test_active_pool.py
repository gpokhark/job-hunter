"""Unit tests for job_hunter.active_pool — migrated from the coverage
tests/test_refilter_archive.py used to carry for its old private `_active_jobs()`, now split
across `raw_active_jobs()` (the unfiltered, optionally multi-source query refilter_archive.py's
own `refilter()` still needs) and `source_jobs()` (the new, fully-filtered, single-source entry
point render_radar.py's stale-source fallback calls) — see
docs/pipeline-refilter-stale-source-plan.md section 4.4."""

from datetime import UTC, datetime

from job_hunter.active_pool import StoredJob, find_jobs, raw_active_jobs, source_jobs
from job_hunter.config import CandidateProfile
from job_hunter.models import Assessment, Job, LocationConfidence
from job_hunter.storage import Storage


def make_job(**updates):
    values = dict(
        source_key="apple", source_platform="test", company="Apple", job_id="1",
        title="Design Verification Engineer", url="https://example.com/1",
        us_eligible=True, location_confidence=LocationConfidence.HIGH,
    )
    values.update(updates)
    return Job(**values)


def test_raw_active_jobs_returns_every_active_us_eligible_job_unfiltered(tmp_path):
    """No prefilter/recency applied at all — this is the raw pool refilter_archive.py's own
    filtering loop still needs so it can tell a prefilter failure apart from a recency one."""
    db_path = tmp_path / "jobs.sqlite3"
    with Storage(db_path) as storage:
        storage.upsert_job(make_job(job_id="1", title="Totally Unrelated Role"))
        storage.upsert_job(make_job(job_id="2", posted_at=datetime(2020, 1, 1, tzinfo=UTC)))

    jobs = raw_active_jobs(db_path)
    assert {job.job_id for job in jobs} == {"1", "2"}


def test_raw_active_jobs_restricts_to_source_scope(tmp_path):
    db_path = tmp_path / "jobs.sqlite3"
    with Storage(db_path) as storage:
        storage.upsert_job(make_job(source_key="apple", job_id="1"))
        storage.upsert_job(make_job(source_key="waymo", job_id="2"))

    jobs = raw_active_jobs(db_path, {"apple"})
    assert [job.source_key for job in jobs] == ["apple"]


def test_raw_active_jobs_attaches_prior_assessment_only_when_content_hash_matches(tmp_path):
    db_path = tmp_path / "jobs.sqlite3"
    with Storage(db_path) as storage:
        storage.upsert_job(make_job(job_id="1", content_hash="abc"))
        storage.upsert_assessment(
            Assessment(
                source_key="apple", job_id="1", company="Apple", title="x",
                url="https://example.com/1", score=80, recommended=True, content_hash="abc",
            )
        )
        storage.upsert_job(make_job(job_id="2", content_hash="stale-now"))
        storage.upsert_assessment(
            Assessment(
                source_key="apple", job_id="2", company="Apple", title="x",
                url="https://example.com/2", score=80, recommended=True,
                content_hash="stale-assessment",
            )
        )

    jobs = {job.job_id: job for job in raw_active_jobs(db_path)}
    assert jobs["1"].prior_assessment is not None
    assert jobs["1"].prior_assessment.score == 80
    assert jobs["2"].prior_assessment is None  # content_hash mismatch -> treated as unassessed


def test_source_jobs_applies_prefilter_and_recency_scoped_to_one_source(tmp_path):
    """The behavior render_radar.py's stale-source fallback actually depends on: a single
    source's pool, already filtered by both checks, with no other source's jobs mixed in even
    if they'd also pass."""
    db_path = tmp_path / "jobs.sqlite3"
    with Storage(db_path) as storage:
        storage.upsert_job(
            make_job(source_key="waymo", job_id="1", title="AV Perception Engineer")
        )
        storage.upsert_job(
            make_job(source_key="waymo", job_id="2", title="Totally Unrelated Role")
        )
        # Same title as job 1, but a different source — must never leak into waymo's result.
        storage.upsert_job(
            make_job(source_key="apple", job_id="3", title="AV Perception Engineer")
        )

    profile = CandidateProfile(target_domains=["perception"])
    jobs = source_jobs(db_path, "waymo", profile, 30, now=datetime(2026, 9, 5, tzinfo=UTC))
    assert [job.job_id for job in jobs] == ["1"]


def test_source_jobs_excludes_stale_postings(tmp_path):
    db_path = tmp_path / "jobs.sqlite3"
    with Storage(db_path) as storage:
        storage.upsert_job(
            make_job(
                source_key="waymo", job_id="1", title="Engineer",
                posted_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )

    profile = CandidateProfile(target_domains=["engineer"])
    jobs = source_jobs(db_path, "waymo", profile, 30, now=datetime(2026, 9, 5, tzinfo=UTC))
    assert jobs == []


def test_source_jobs_honors_a_keyword_override(tmp_path):
    """`keywords`, when given, fully replaces the profile's own positive-match terms for this
    one call — same meaning as `job-hunter search --keyword` everywhere else in this project,
    not a narrowing of the profile's terms."""
    db_path = tmp_path / "jobs.sqlite3"
    with Storage(db_path) as storage:
        storage.upsert_job(make_job(source_key="waymo", job_id="1", title="Robotics Lead"))

    profile = CandidateProfile(target_domains=["something-unrelated"])
    now = datetime(2026, 9, 5, tzinfo=UTC)
    assert source_jobs(db_path, "waymo", profile, 30, now=now) == []
    matched = source_jobs(db_path, "waymo", profile, 30, keywords=["robotics"], now=now)
    assert [job.job_id for job in matched] == ["1"]


def test_source_jobs_returns_empty_for_a_source_with_no_active_jobs(tmp_path):
    db_path = tmp_path / "jobs.sqlite3"
    with Storage(db_path):
        pass  # just initialize the schema, no jobs stored at all

    profile = CandidateProfile(target_domains=["engineer"])
    assert source_jobs(db_path, "waymo", profile, 30) == []


def _seed_lookup(tmp_path):
    db = tmp_path / "jobs.sqlite3"
    with Storage(db) as storage:
        storage.upsert_job(
            make_job(
                source_key="ford", job_id="71202", title="Vehicle Calibration & Test Supervisor",
                url="https://efds.example/hcmUI/job/71202",
            )
        )
        storage.upsert_job(
            make_job(
                source_key="ford", job_id="71203", title="Calibration Engineer",
                url="https://efds.example/hcmUI/job/71203",
            )
        )
        storage.upsert_job(
            make_job(source_key="abb", job_id="71202", title="Test Supervisor", url="https://abb.example/j/71202")
        )
    return db


def test_find_jobs_by_source_and_id(tmp_path):
    found = find_jobs(_seed_lookup(tmp_path), "ford:71202")
    assert [(s.job.source_key, s.job.job_id) for s in found] == [("ford", "71202")]
    assert isinstance(found[0], StoredJob)
    assert (found[0].status, found[0].missing_count) == ("active", 0)


def test_find_jobs_by_exact_canonical_url(tmp_path):
    found = find_jobs(_seed_lookup(tmp_path), "https://efds.example/hcmUI/job/71203")
    assert [s.job.job_id for s in found] == ["71203"]


def test_find_jobs_by_an_id_token_inside_a_different_url_shape(tmp_path):
    db = _seed_lookup(tmp_path)
    found = find_jobs(db, "https://efds.example/careers/job/-/-/48560/71203")
    assert [(s.job.source_key, s.job.job_id) for s in found] == [("ford", "71203")]
    # a plain number on a host no stored job uses could be any source's id
    assert find_jobs(db, "https://careers.example.com/job/-/-/48560/71203") == []


def test_find_jobs_url_with_no_matching_id_returns_nothing(tmp_path):
    """The real Ford careers link carries ids that are not the stored job id (71202)."""
    ref = "https://www.careers.ford.com/job/-/-/48560/101370456832"
    assert find_jobs(_seed_lookup(tmp_path), ref) == []


def test_find_jobs_bare_id_can_match_several_sources(tmp_path):
    found = find_jobs(_seed_lookup(tmp_path), "71202")
    assert sorted(s.job.source_key for s in found) == ["abb", "ford"]


def test_find_jobs_by_title_substring_is_case_insensitive(tmp_path):
    db = _seed_lookup(tmp_path)
    assert sorted(s.job.source_key for s in find_jobs(db, "test supervisor")) == ["abb", "ford"]
    assert [s.job.job_id for s in find_jobs(db, "VEHICLE CALIBRATION & TEST")] == ["71202"]


def test_find_jobs_includes_closed_and_ineligible_jobs(tmp_path):
    db = _seed_lookup(tmp_path)
    with Storage(db) as storage:
        storage.connection.execute(
            "UPDATE jobs SET status='closed', missing_count=3, us_eligible=0 "
            "WHERE source_key='ford' AND job_id='71203'"
        )
        storage.connection.commit()
    (found,) = find_jobs(db, "ford:71203")
    assert (found.status, found.missing_count, found.job.us_eligible) == ("closed", 3, False)


def test_find_jobs_blank_unknown_and_wildcard_refs_return_nothing(tmp_path):
    db = _seed_lookup(tmp_path)
    assert find_jobs(db, "") == []
    assert find_jobs(db, "   ") == []
    assert find_jobs(db, "nothing like this anywhere") == []
    assert find_jobs(db, "%") == []  # a literal percent sign, not a SQL wildcard


def test_find_jobs_title_with_a_colon_and_space_is_not_read_as_source_id(tmp_path):
    db = tmp_path / "jobs.sqlite3"
    with Storage(db) as storage:
        storage.upsert_job(make_job(job_id="9", title="Engineer: Perception Systems"))
    assert [s.job.job_id for s in find_jobs(db, "Engineer: Perception")] == ["9"]


def _seed_tokens(tmp_path):
    db = tmp_path / "tok.sqlite3"
    with Storage(db) as storage:
        storage.upsert_job(
            make_job(source_key="hexco", job_id="a148560f9c2", title="Hash Id Job",
                     url="https://hexco.example/j/zzz")
        )
        storage.upsert_job(
            make_job(source_key="idco", job_id="48560", title="Exact Id Job",
                     url="https://idco.example/j/other")
        )
        storage.upsert_job(
            make_job(source_key="segco", job_id="s1", title="Segment Job",
                     url="https://segco.example/careers/48560/apply")
        )
        storage.upsert_job(
            make_job(source_key="longco", job_id="l1", title="Longer Run Job",
                     url="https://longco.example/careers/9485601/apply")
        )
    return db


def test_find_jobs_url_token_does_not_match_inside_a_hex_job_id(tmp_path):
    db = _seed_tokens(tmp_path)
    found = find_jobs(db, "https://www.careers.ford.com/job/-/-/48560/101370456832")
    assert "hexco" not in {s.job.source_key for s in found}


def test_find_jobs_url_token_matches_job_id_exactly(tmp_path):
    db = _seed_tokens(tmp_path)
    found = find_jobs(db, "https://idco.example/job/-/-/101370/48560")
    assert "idco" in {s.job.source_key for s in found}


def test_find_jobs_url_token_matches_bounded_canonical_url_segment_only(tmp_path):
    db = _seed_tokens(tmp_path)
    # an exact job_id beats canonical-url token matches
    assert {s.job.source_key for s in find_jobs(db, "https://idco.example/job/48560")} == {"idco"}
    with Storage(db) as storage:
        storage.connection.execute("DELETE FROM jobs WHERE source_key='idco'")
        storage.connection.commit()
    found = {s.job.source_key for s in find_jobs(db, "https://segco.example/job/48560")}
    assert found == {"segco"}  # not longco (9485601) nor hexco (a148560f9c2)


def test_find_jobs_url_token_inside_longer_digit_run_alone_matches_nothing(tmp_path):
    db = tmp_path / "only.sqlite3"
    with Storage(db) as storage:
        storage.upsert_job(
            make_job(source_key="longco", job_id="l1", title="Longer Run Job",
                     url="https://longco.example/careers/9485601/apply")
        )
    assert find_jobs(db, "https://x.example/job/48560") == []


def test_find_jobs_url_token_alone_does_not_match_hex_hash_in_job_id_or_url(tmp_path):
    db = tmp_path / "hex.sqlite3"
    with Storage(db) as storage:
        storage.upsert_job(
            make_job(source_key="hexco", job_id="a148560f9c2", title="Hash Id Job",
                     url="https://hexco.example/j/b48560c")
        )
    assert find_jobs(db, "https://x.example/job/-/-/48560/101370456832") == []


def test_find_jobs_url_uses_only_the_last_digit_run(tmp_path):
    db = tmp_path / "last.sqlite3"
    with Storage(db) as storage:
        storage.upsert_job(
            make_job(source_key="ford", job_id="h1", title="Shared Segment Job",
                     url="https://www.careers.ford.com/job/city/title/48560/99653551168")
        )
    assert find_jobs(db, "https://www.careers.ford.com/job/-/-/48560/101370456832") == []


def test_find_jobs_url_last_token_matches_even_when_earlier_token_is_shared(tmp_path):
    db = tmp_path / "shared.sqlite3"
    with Storage(db) as storage:
        storage.upsert_job(
            make_job(source_key="ford", job_id="h1", title="Other",
                     url="https://www.careers.ford.com/job/city/title/48560/99653551168")
        )
        storage.upsert_job(
            make_job(source_key="ford", job_id="101370456832", title="Target",
                     url="https://elsewhere.example/x")
        )
    found = find_jobs(db, "https://www.careers.ford.com/job/-/-/48560/101370456832")
    assert [s.job.job_id for s in found] == ["101370456832"]


# --- URL resolution across real platform shapes (stored job_id is often a hash; the real id
# lives in the canonical URL's last segment or its query string) ---

WD = "https://cat.wd5.myworkdayjobs.com/en-US/CaterpillarCareers/job/Godollo-Budapest/Mrnk-gyakornok_R0000391568"
WD_JR = "https://stoneridge.wd5.myworkdayjobs.com/en-US/Careers/job/Barneveld-Netherlands/Systems-Engineer_JR102607"
ASHBY = "https://jobs.ashbyhq.com/openai/000cf0f2-090d-40c6-b9f8-1699db9a4c68"
LEVER = "https://jobs.lever.co/zoox/000392a0-3844-47c4-b580-d56d9a97620c"
APPLE = "https://jobs.apple.com/en-us/details/200462446-0836/product-design-engineer-iphone"


def _seed_shapes(tmp_path):
    db = tmp_path / "shapes.sqlite3"
    with Storage(db) as storage:
        rows = [
            ("caterpillar", "hash-cat-1", WD),
            ("caterpillar", "hash-cat-2", WD.replace("_R0000391568", "_R0000391569")),
            ("stoneridge", "hash-sr-1", WD_JR),
            ("stoneridge", "hash-sr-2", WD_JR.replace("JR102607", "JR102608")),
            ("openai", "000cf0f2-090d-40c6-b9f8-1699db9a4c68", ASHBY),
            ("zoox", "000392a0-3844-47c4-b580-d56d9a97620c", LEVER),
            ("waymo", "6499165", "https://careers.withwaymo.com/jobs?gh_jid=6499165"),
            ("waymo", "6499166", "https://careers.withwaymo.com/jobs?gh_jid=6499166"),
            ("fanuc", "5001120355006", "https://myjobs.adp.com/fanuc/cx/job-details?reqId=5001120355006"),
            ("fanuc", "5001120355007", "https://myjobs.adp.com/fanuc/cx/job-details?reqId=5001120355007"),
            ("apple", "200462446-0836", APPLE),
            ("apple", "200462447-0836", "https://jobs.apple.com/en-us/details/200462447-0836/other-role"),
            ("apple", "200462448-0836", "https://jobs.apple.com/en-us/details/200462448-0836/third-role"),
            ("rivian", "19528", "https://careers.rivian.com/careers-home/jobs/19528"),
            ("other", "x1", "https://other.example/careers/19528-something/apply"),
            ("bosch", "REF1018E", "https://jobs.bosch.com/en/job/REF1018E-industrial-maintenance-technician"),
        ]
        for source, job_id, url in rows:
            storage.upsert_job(make_job(source_key=source, job_id=job_id, title=f"{source} {job_id}", url=url))
    return db


def _keys(found):
    return [(s.job.source_key, s.job.job_id) for s in found]


def test_find_jobs_url_with_tracking_query_and_trailing_slash_resolves_exactly(tmp_path):
    db = _seed_shapes(tmp_path)
    assert _keys(find_jobs(db, WD + "?utm_source=share")) == [("caterpillar", "hash-cat-1")]
    assert _keys(find_jobs(db, WD + "/")) == [("caterpillar", "hash-cat-1")]
    assert _keys(find_jobs(db, WD.replace("https://cat.", "HTTPS://CAT.") + "#top")) == [("caterpillar", "hash-cat-1")]


def test_find_jobs_workday_r_and_jr_ids_in_a_different_url_shape(tmp_path):
    db = _seed_shapes(tmp_path)
    other = "https://cat.wd5.myworkdayjobs.com/CaterpillarCareers/job/Elsewhere/Some-Title_R0000391568?source=x"
    assert _keys(find_jobs(db, other)) == [("caterpillar", "hash-cat-1")]
    jr = "https://stoneridge.wd5.myworkdayjobs.com/Careers/details/Systems-Engineer_JR102607"
    assert _keys(find_jobs(db, jr)) == [("stoneridge", "hash-sr-1")]


def test_find_jobs_ashby_and_lever_uuid_ids(tmp_path):
    db = _seed_shapes(tmp_path)
    assert _keys(find_jobs(db, ASHBY + "/application?utm_source=share")) == [
        ("openai", "000cf0f2-090d-40c6-b9f8-1699db9a4c68")
    ]
    assert _keys(find_jobs(db, LEVER + "?lever-source=x")) == [("zoox", "000392a0-3844-47c4-b580-d56d9a97620c")]


def test_find_jobs_ids_in_the_query_string(tmp_path):
    db = _seed_shapes(tmp_path)
    assert _keys(find_jobs(db, "https://careers.withwaymo.com/jobs/?gh_jid=6499166&utm_source=share")) == [
        ("waymo", "6499166")
    ]
    assert _keys(find_jobs(db, "https://myjobs.adp.com/fanuc/cx/job-details?lang=en&reqId=5001120355007")) == [
        ("fanuc", "5001120355007")
    ]


def test_find_jobs_apple_digits_dash_digits_id_is_matched_whole_not_by_its_shared_tail(tmp_path):
    db = _seed_shapes(tmp_path)
    assert _keys(find_jobs(db, APPLE + "?team=SFTWR")) == [("apple", "200462446-0836")]
    assert _keys(find_jobs(db, APPLE + "/")) == [("apple", "200462446-0836")]
    unknown = "https://jobs.apple.com/en-us/details/299999999-0836/unknown-role"
    assert find_jobs(db, unknown) == []


def test_find_jobs_exact_job_id_takes_precedence_over_url_substring_matches(tmp_path):
    db = _seed_shapes(tmp_path)
    assert _keys(find_jobs(db, "https://careers.rivian.com/careers-home/jobs/19528?lang=en-us")) == [
        ("rivian", "19528")
    ]


def test_find_jobs_alphanumeric_id_prefix_of_a_slug_segment(tmp_path):
    db = _seed_shapes(tmp_path)
    found = find_jobs(db, "https://jobs.bosch.com/en/job/REF1018E-renamed-title?utm_source=share")
    assert _keys(found) == [("bosch", "REF1018E")]


def test_find_jobs_url_never_falls_through_to_a_title_match(tmp_path):
    db = _seed_shapes(tmp_path)
    assert find_jobs(db, "https://nowhere.example/rivian") == []


def test_find_jobs_results_are_deterministically_ordered(tmp_path):
    db = _seed_lookup(tmp_path)
    assert _keys(find_jobs(db, "71202")) == [("abb", "71202"), ("ford", "71202")]


def test_raw_active_jobs_readonly_never_writes_or_migrates_the_database(tmp_path):
    import os
    import sqlite3

    db = tmp_path / "jobs.sqlite3"
    with Storage(db) as storage:
        storage.upsert_job(make_job())
        storage.connection.execute("PRAGMA user_version = 1")
        storage.connection.commit()
    # fold the WAL in so the main file is the whole database
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.close()
    before = (db.read_bytes(), os.stat(db).st_mtime_ns)
    jobs = raw_active_jobs(db, readonly=True)
    assert [j.job_id for j in jobs] == ["1"]
    assert raw_active_jobs(db, {"apple"}, readonly=True)[0].source_key == "apple"
    assert (db.read_bytes(), os.stat(db).st_mtime_ns) == before
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    conn.close()


def _one_job(tmp_path, source, job_id, url):
    db = tmp_path / f"{source}.sqlite3"
    with Storage(db) as storage:
        storage.upsert_job(make_job(source_key=source, job_id=job_id, title=f"{source} job", url=url))
    return db


def test_find_jobs_query_less_form_is_used_only_for_a_ref_without_an_id_query(tmp_path):
    db = _one_job(tmp_path, "waymo", "6499165", "https://careers.withwaymo.com/jobs?gh_jid=6499165")
    assert find_jobs(db, "https://careers.withwaymo.com/jobs?gh_jid=9999999") == []
    assert _keys(find_jobs(db, "https://careers.withwaymo.com/jobs?gh_jid=6499165&utm_source=x")) == [
        ("waymo", "6499165")
    ]
    apple = _one_job(tmp_path, "apple", "200462446-0836", APPLE)
    # only tracking params: genuinely the bare form, so the query-less compare applies
    assert _keys(find_jobs(apple, APPLE + "?utm_source=share&gclid=1234")) == [("apple", "200462446-0836")]


def test_find_jobs_plain_ids_on_an_unknown_host_never_pick_another_sources_job(tmp_path):
    db = _one_job(tmp_path, "zz", "12345", "https://zz.example/jobs/12345")
    assert find_jobs(db, "https://other.example/job?page=12345") == []
    assert find_jobs(db, "https://other.example/careers/job/12345") == []
    # the same plain id on the job's own host still resolves
    assert _keys(find_jobs(db, "https://zz.example/careers/job/12345")) == [("zz", "12345")]


def test_find_jobs_unambiguous_id_shapes_still_resolve_from_an_unknown_host(tmp_path):
    db = _seed_shapes(tmp_path)
    assert _keys(find_jobs(db, "https://mirror.example/x/000cf0f2-090d-40c6-b9f8-1699db9a4c68")) == [
        ("openai", "000cf0f2-090d-40c6-b9f8-1699db9a4c68")
    ]
    assert _keys(find_jobs(db, "https://mirror.example/x/Some-Title_JR102607")) == [("stoneridge", "hash-sr-1")]
    assert _keys(find_jobs(db, "https://mirror.example/x/200462446-0836")) == [("apple", "200462446-0836")]
