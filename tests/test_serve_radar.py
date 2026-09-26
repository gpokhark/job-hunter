import http.client
import json
import os
import sys
import threading
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
    for path in ("/nope", "/data/jobs.sqlite3", "/../etc/passwd", "/api", "/applications"):
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
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.yaml").write_text(f"database_path: {tmp_path}/data/jobs.sqlite3\n")
    monkeypatch.chdir(tmp_path)
    env_dir = tmp_path / "e"
    env_dir.mkdir()
    e = Env(env_dir)
    try:
        assert serve_radar.main(["--port", str(e.port), "--host", "127.0.0.1"]) == 2
        assert "cannot listen" in capsys.readouterr().err
    finally:
        e.close()
    with run_lock("radar-server"):
        assert serve_radar.main(["--port", "0"]) == 2
