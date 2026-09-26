import http.client
import json
import os
import socket
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import serve_radar  # noqa: E402

from job_hunter.config import Settings
from job_hunter.models import Assessment, Job, LocationConfidence
from job_hunter.runlock import run_lock
from job_hunter.storage import Storage


def _job(job_id, title="Engineer", company="Acme"):
    return Job(
        source_key="acme", source_platform="test", company=company, job_id=job_id, title=title,
        url=f"https://example.com/{job_id}", us_eligible=True,
        location_confidence=LocationConfidence.HIGH, description="secret body", content_hash="h",
    )


def _candidate(job_id, title, company="Acme"):
    return {
        "source_key": "acme", "job_id": job_id, "posted_at": "2026-09-20T00:00:00Z",
        "first_seen_at": None, "location_raw": "Detroit, MI", "visa_sponsorship": "unmentioned",
        "sponsorship_evidence": None, "salary_evidence": None, "work_arrangement": "unknown",
        "company": company, "title": title, "url": f"https://example.com/{job_id}",
        "state": "MI", "country": "US",
    }


def _ts(seconds_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds_ago)).isoformat()


class Env:
    def __init__(self, tmp_path):
        self.root = tmp_path
        self.db = tmp_path / "data" / "jobs.sqlite3"
        self.archive = tmp_path / "data" / "searches" / "default_2026-09-26.json"
        self.assessments = tmp_path / "data" / "assessments.json"
        self.archive.parent.mkdir(parents=True)
        self.archive.write_text(json.dumps({
            "summary": {"sources_attempted": 3, "sources_succeeded": 3},
            "candidates": [_candidate("1", "ADAS Engineer"), _candidate("2", "Other Role")],
            "source_health": [],
        }))
        self.assessments.write_text(json.dumps([{
            "source_key": "acme", "job_id": "1", "score": 82, "company": "Acme",
            "title": "ADAS Engineer", "url": "https://example.com/1", "matches": ["m"], "gaps": ["g"],
        }]))
        with Storage(self.db) as storage:
            storage.upsert_job(_job("1", "ADAS Engineer"))
            storage.upsert_job(_job("2", "Other Role"))
            storage.upsert_assessment(Assessment(
                source_key="acme", job_id="1", company="Acme", title="ADAS Engineer",
                url="https://example.com/1", content_hash="h", score=82, recommended=True,
                matches=["m"], gaps=["g"],
            ))
        args = serve_radar.build_parser().parse_args([
            "--search", str(self.archive), "--assessments", str(self.assessments), "--port", "0",
        ])
        cfg = serve_radar.ServerConfig(
            args=args, settings=Settings(database_path=self.db), profile_loader=lambda: None
        )
        self.server = serve_radar.RadarServer(cfg)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def request(self, method, path, body=None, headers=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        hdrs = dict(headers or {})
        payload = raw
        if body is not None:
            payload = json.dumps(body)
            hdrs.setdefault("Content-Type", "application/json")
        conn.request(method, path, body=payload, headers=hdrs)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        parsed = None
        if "json" in (response.getheader("Content-Type") or ""):
            parsed = json.loads(data)
        return response.status, response, parsed if parsed is not None else data.decode("utf-8")

    def post_feedback(self, label, seconds_ago, job_id="1", **extra):
        body = {"source_key": "acme", "job_id": job_id, "label": label, "client_ts": _ts(seconds_ago)}
        body.update(extra)
        return self.request("POST", "/api/feedback", body)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.close()


def test_root_serves_a_live_page_without_writing_radar_files(env):
    status, response, html = env.request("GET", "/")
    assert status == 200
    assert response.getheader("Cache-Control") == "no-store"
    assert "window.__RADAR_LIVE__" in html and "ADAS Engineer" in html
    assert not list((env.root / "data").glob("radar*"))


def test_unknown_routes_and_data_files_are_never_served(env):
    for path in ("/nope", "/data/jobs.sqlite3", "/../etc/passwd", "/api", "/applications/x"):
        status, _, _ = env.request("GET", path)
        assert status == 404, path


def test_unsupported_methods_are_rejected(env):
    for method in ("PUT", "DELETE", "PATCH"):
        status, _, _ = env.request(method, "/api/feedback", body={})
        assert status == 405


def test_feedback_create_relabel_and_untag_round_trip(env):
    status, _, body = env.post_feedback("okay", 30)
    assert status == 200 and body["item"]["label"] == "okay"
    assert env.request("GET", "/api/feedback")[2]["feedback"]["acme|1"]["label"] == "okay"
    status, _, body = env.post_feedback("irrelevant", 20)
    assert body["item"]["label"] == "irrelevant"
    status, _, body = env.post_feedback(None, 10)
    assert status == 200 and body == {"ok": True, "deleted": True}
    assert env.request("GET", "/api/feedback")[2]["feedback"] == {}


def test_server_derives_snapshot_fields_and_rejects_client_supplied_ones(env):
    status, _, body = env.post_feedback("okay", 5, company="Evil Corp")
    assert status == 400  # extra="forbid": client metadata is never accepted
    env.post_feedback("okay", 5)
    with Storage(env.db) as storage:
        row = storage.get_job_feedback("acme", "1")
    assert (row["company"], row["title"], row["score"]) == ("Acme", "ADAS Engineer", 82)


def test_late_retry_cannot_overwrite_a_newer_edit(env):
    env.post_feedback("okay", 30)
    status, _, body = env.post_feedback("irrelevant", 60)  # older than what's stored
    assert status == 200 and body["stale"] is True and body["item"]["label"] == "okay"
    assert env.post_feedback("irrelevant", 1)[2]["item"]["label"] == "irrelevant"


def test_a_stale_label_cannot_resurrect_an_untagged_job(env):
    env.post_feedback("okay", 40)
    env.post_feedback(None, 20)
    status, _, body = env.post_feedback("relevant", 30)
    assert body["stale"] is True and body["item"] is None
    assert env.request("GET", "/api/feedback")[2]["feedback"] == {}


def test_unknown_job_is_404_but_untagging_an_existing_label_of_a_removed_job_works(env):
    assert env.post_feedback("okay", 5, job_id="nope")[0] == 404
    assert env.post_feedback(None, 5, job_id="nope")[0] == 404
    env.post_feedback("okay", 30)
    with Storage(env.db) as storage:  # job later removed by `cleanup`
        storage.connection.execute("DELETE FROM jobs WHERE job_id='1'")
        storage.connection.commit()
    assert env.post_feedback("okay", 5)[0] == 404  # tagging a job that is gone is refused...
    assert env.post_feedback(None, 5)[0] == 200  # ...but a stale tab can still untag it


def test_request_validation_errors(env):
    assert env.request("POST", "/api/feedback", raw="{not json", headers={"Content-Type": "application/json"})[0] == 400
    assert env.request("POST", "/api/feedback", body=[1, 2])[0] == 400
    assert env.post_feedback("maybe", 5)[0] == 400
    assert env.post_feedback("okay", -3600)[0] == 400  # client_ts in the future
    naive = {"source_key": "acme", "job_id": "1", "label": "okay", "client_ts": "2026-09-26T12:00:00"}
    assert env.request("POST", "/api/feedback", naive)[0] == 400
    blank = {"source_key": " ", "job_id": "1", "label": "okay", "client_ts": _ts(1)}
    assert env.request("POST", "/api/feedback", blank)[0] == 400
    missing_label = {"source_key": "acme", "job_id": "1", "client_ts": _ts(1)}
    assert env.request("POST", "/api/feedback", missing_label)[0] == 400


def test_request_guards_content_type_origin_host_and_body_size(env):
    payload = json.dumps({"source_key": "acme", "job_id": "1", "label": "okay", "client_ts": _ts(1)})
    assert env.request("POST", "/api/feedback", raw=payload, headers={"Content-Type": "text/plain"})[0] == 415
    host = f"127.0.0.1:{env.port}"
    ok_headers = {"Content-Type": "application/json", "Origin": f"http://{host}"}
    assert env.request("POST", "/api/feedback", raw=payload, headers=ok_headers)[0] == 200
    bad_origin = {"Content-Type": "application/json", "Origin": "http://evil.example"}
    assert env.request("POST", "/api/feedback", raw=payload, headers=bad_origin)[0] == 403
    assert env.request("GET", "/api/state", headers={"Host": "evil.example"})[0] == 403
    big = "x" * (serve_radar.MAX_BODY_BYTES + 1)
    assert env.request("POST", "/api/feedback", raw=big, headers={"Content-Type": "application/json"})[0] == 413
    response_headers = env.request("GET", "/api/state")[1]
    assert response_headers.getheader("Access-Control-Allow-Origin") is None


def test_state_versions_change_with_archive_assessments_and_feedback(env):
    v0 = env.request("GET", "/api/state")[2]["versions"]
    env.post_feedback("okay", 30)
    v1 = env.request("GET", "/api/state")[2]
    assert v1["versions"]["feedback"] != v0["feedback"]
    assert v1["counts"]["feedback"] == 1 and v1["archive_name"] == env.archive.name
    env.post_feedback("irrelevant", 10)  # same row count, different label
    v2 = env.request("GET", "/api/state")[2]["versions"]
    assert v2["feedback"] != v1["versions"]["feedback"]
    env.assessments.write_text(env.assessments.read_text() + " ")
    assert env.request("GET", "/api/state")[2]["versions"]["assessments"] != v0["assessments"]
    data = json.loads(env.archive.read_text())
    data["candidates"].append(_candidate("3", "New Role"))
    env.archive.write_text(json.dumps(data))
    assert env.request("GET", "/api/state")[2]["versions"]["archive"] != v0["archive"]


def test_missing_archive_is_a_clear_503_not_a_crash(env):
    os.remove(env.archive)
    status, _, body = env.request("GET", "/")
    assert status == 503 and "does not exist" in body["error"]
    assert env.request("GET", "/api/state")[2]["archive_name"] is None


def test_display_host():
    d = serve_radar.display_host
    assert d("127.0.0.1") == "127.0.0.1"
    assert d("localhost") == "localhost"
    assert d("0.0.0.0") == "127.0.0.1"
    assert d("::") == "[::1]"
    assert d("::1") == "[::1]"
    assert d("192.168.1.5") == "192.168.1.5"
    assert d("fe80::1") == "[fe80::1]"


def test_deeply_nested_json_is_a_400_and_server_survives(env):
    raw = "[" * 60000
    status, _, body = env.request("POST", "/api/feedback", raw=raw, headers={"Content-Type": "application/json"})
    assert status == 400 and body["ok"] is False
    assert env.request("GET", "/api/state")[0] == 200


def test_allowed_hosts_rules():
    assert serve_radar.allowed_hosts("127.0.0.1", 8765) == {
        "127.0.0.1:8765", "localhost:8765", "[::1]:8765"
    }
    wildcard = serve_radar.allowed_hosts("0.0.0.0", 9, ["Radar.lan:9"])
    assert "radar.lan:9" in wildcard and "0.0.0.0:9" not in wildcard
    assert serve_radar.allowed_hosts("192.168.1.5", 9) == {"192.168.1.5:9"}


def test_non_loopback_warning():
    assert serve_radar.non_loopback_warning("127.0.0.1") is None
    assert serve_radar.non_loopback_warning("localhost") is None
    assert "unauthenticated" in serve_radar.non_loopback_warning("0.0.0.0")


def test_port_collision_and_held_lock_are_clean_failures(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("JOB_HUNTER_ROOT", raising=False)
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.yaml").write_text(f"database_path: {tmp_path}/data/jobs.sqlite3\n")
    monkeypatch.chdir(tmp_path)
    env_dir = tmp_path / "e"
    env_dir.mkdir()
    e = Env(env_dir)
    try:
        assert serve_radar.main(["--project", str(tmp_path), "--port", str(e.port), "--host", "127.0.0.1"]) == 2
        assert "cannot listen" in capsys.readouterr().err
    finally:
        e.close()
    with run_lock("radar-server"):
        assert serve_radar.main(["--project", str(tmp_path), "--port", "0"]) == 2


def post_app(env, seconds_ago, job_id="1", **fields):
    body = {"source_key": "acme", "job_id": job_id, "client_ts": _ts(seconds_ago)}
    body.update(fields)
    return env.request("POST", "/api/application", body)


def test_application_create_update_and_delete_round_trip(env):
    status, _, body = post_app(env, 60, status="applied", notes="referral")
    assert status == 200 and body["ok"] is True
    item = body["item"]
    assert (item["status"], item["notes"], item["company"], item["score"]) == ("applied", "referral", "Acme", 82)
    assert item["applied_at"] == datetime.now().date().isoformat()  # defaults to the server's local today
    assert "description" not in json.dumps(body)
    # partial update: only the notes; status/date unchanged
    _, _, body = post_app(env, 30, notes="second call")
    assert (body["item"]["status"], body["item"]["notes"]) == ("applied", "second call")
    # list endpoint and state version
    listing = env.request("GET", "/api/applications")[2]
    assert listing["applications"]["acme|1"]["notes"] == "second call"
    # delete
    status, _, body = post_app(env, 5, status=None)
    assert status == 200 and body == {"ok": True, "deleted": True}
    assert env.request("GET", "/api/applications")[2]["applications"] == {}


def test_application_snapshot_is_server_derived_and_client_metadata_is_rejected(env):
    assert post_app(env, 5, status="saved", company="Evil Corp")[0] == 400  # extra="forbid"
    assert post_app(env, 4, status="saved", url="https://evil.example")[0] == 400
    post_app(env, 3, status="saved")
    with Storage(env.db) as storage:
        row = storage.get_application("acme", "1")
    assert (row["company"], row["title"], row["url"]) == ("Acme", "ADAS Engineer", "https://example.com/1")


def test_application_validation_errors(env):
    assert post_app(env, 5)[0] == 400  # nothing to change
    assert post_app(env, 5, status="hired")[0] == 400
    assert post_app(env, 5, status="saved", applied_at="2026-09-01")[0] == 400  # saved has no date
    assert post_app(env, 5, notes="no status yet")[0] == 400  # creating needs a status
    assert post_app(env, 5, status="applied", applied_at="2026-02-30")[0] == 400  # not a real date
    assert post_app(env, 5, status="applied", notes="x" * 4001)[0] == 400
    post_app(env, 4, status="applied")
    assert post_app(env, 3, applied_at=None)[0] == 400  # a date can never be cleared
    assert post_app(env, -3600, status="applied")[0] == 400  # future client_ts
    assert env.request("GET", "/api/applications")[2]["applications"]["acme|1"]["status"] == "applied"


def test_application_unknown_job_is_404_but_deleting_a_known_deletion_is_idempotent(env):
    assert post_app(env, 5, job_id="nope", status="saved")[0] == 404
    assert post_app(env, 5, job_id="nope", status=None)[0] == 404
    post_app(env, 60, status="applied")
    with Storage(env.db) as storage:  # job later removed by `cleanup`
        storage.connection.execute("DELETE FROM jobs WHERE job_id='1'")
        storage.connection.commit()
    assert post_app(env, 30, notes="still editable")[0] == 200  # existing snapshot is enough
    assert post_app(env, 20, status=None)[0] == 200
    assert post_app(env, 10, status=None)[0] == 200  # repeated delete of a removed job converges
    assert post_app(env, 5, status="saved")[0] == 404  # ...but a removed job cannot be newly tracked


def test_late_application_writes_cannot_overwrite_newer_edits_or_resurrect_deleted_rows(env):
    post_app(env, 30, status="applied", notes="newer")
    status, _, body = post_app(env, 60, status="offer", notes="older")
    assert status == 200 and body["stale"] is True and body["item"]["status"] == "applied"
    post_app(env, 20, status=None)
    status, _, body = post_app(env, 25, status="applied")
    assert body["stale"] is True and body["item"] is None
    assert env.request("GET", "/api/applications")[2]["applications"] == {}


def test_application_writes_refresh_both_export_files(env):
    post_app(env, 10, status="applied", notes="=HYPERLINK(\"http://evil\")")
    data = env.root / "data"
    exported = json.loads((data / "applications.json").read_text())
    assert exported[0]["job_id"] == "1" and exported[0]["status"] == "applied"
    csv_text = (data / "applications.csv").read_text()
    assert csv_text.splitlines()[0].startswith("source_key,job_id,company")
    assert "'=HYPERLINK" in csv_text  # formula guard
    before = (data / "applications.json").read_text()
    post_app(env, 5, notes="changed")
    assert (data / "applications.json").read_text() != before


def test_export_failure_never_fails_or_reverts_the_save(env, monkeypatch):
    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(serve_radar, "write_applications_exports", boom)
    status, _, body = post_app(env, 10, status="applied")
    assert status == 200 and body["ok"] is True and "OSError" in body["export_warning"]
    assert "disk full" not in body["export_warning"]
    assert body["item"]["status"] == "applied"
    with Storage(env.db) as storage:
        assert storage.get_application("acme", "1")["status"] == "applied"


def test_state_versions_and_counts_include_applications(env):
    v0 = env.request("GET", "/api/state")[2]
    post_app(env, 30, status="applied")
    v1 = env.request("GET", "/api/state")[2]
    assert v1["versions"]["applications"] != v0["versions"]["applications"]
    assert v1["counts"]["applications"] == 1
    post_app(env, 10, notes="same row count, different content")
    v2 = env.request("GET", "/api/state")[2]["versions"]
    assert v2["applications"] != v1["versions"]["applications"]
    assert v2["feedback"] == v0["versions"]["feedback"]  # feedback version is independent


def test_radar_page_boots_with_application_state_and_link(env):
    post_app(env, 30, status="interviewing", notes="phone screen")
    html = env.request("GET", "/")[2]
    assert 'href="/applications"' in html
    assert '"applications": {"acme|1": {"applied_at":' in html
    assert 'data-app-status="interviewing"' in html


def test_applications_page_lists_rows_and_marks_removed_postings(env):
    post_app(env, 30, status="applied", notes="</script><img src=x onerror=alert(1)>")
    post_app(env, 20, job_id="2", status="saved")
    with Storage(env.db) as storage:
        storage.connection.execute("UPDATE jobs SET status='closed' WHERE job_id='2'")
        storage.connection.execute("DELETE FROM jobs WHERE job_id='1'")
        storage.connection.commit()
    status, response, html = env.request("GET", "/applications")
    assert status == 200 and response.getheader("Cache-Control") == "no-store"
    assert 'data-posting="removed"' in html and 'data-posting="closed"' in html
    assert "&lt;/script&gt;&lt;img src=x" in html and "<img src=x" not in html
    assert 'window.__APPS_LIVE__ = {"versions":' in html
    assert "jobs.sqlite3" not in html


def test_applications_page_works_with_no_archive_and_no_applications(env):
    os.remove(env.archive)  # /applications does not depend on the radar archive
    status, _, html = env.request("GET", "/applications")
    assert status == 200 and 'id="app-empty" class="app-empty">' in html


def test_repeated_feedback_untag_of_a_removed_job_is_idempotent(env):
    env.post_feedback("okay", 60)
    with Storage(env.db) as storage:
        storage.connection.execute("DELETE FROM jobs WHERE job_id='1'")
        storage.connection.commit()
    assert env.post_feedback(None, 30)[0] == 200
    assert env.post_feedback(None, 10)[0] == 200  # was a 404 in Phase A
    assert env.post_feedback("okay", 5)[0] == 404  # a removed job still cannot be newly tagged


def test_a_slow_post_body_times_out_instead_of_pinning_a_thread(tmp_path, monkeypatch):
    monkeypatch.setattr(serve_radar.RadarHandler, "timeout", 1)
    e = Env(tmp_path)
    try:
        sock = socket.create_connection(("127.0.0.1", e.port), timeout=10)
        sock.sendall(
            f"POST /api/feedback HTTP/1.1\r\nHost: 127.0.0.1:{e.port}\r\n"
            "Content-Type: application/json\r\nContent-Length: 500\r\n\r\n{".encode()
        )
        started = time.monotonic()
        data = sock.recv(4096)  # the server gives up and closes; it must not wait forever
        assert time.monotonic() - started < 8
        assert data == b"" or b"HTTP/1.1" in data or b"HTTP/1.0" in data
        sock.close()
        assert e.request("GET", "/api/state")[0] == 200  # server still healthy
    finally:
        e.close()
