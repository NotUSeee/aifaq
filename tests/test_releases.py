"""Releases announced by the deploy pipeline.

What this is for: a restart during a release is expected and short. Without
a word from the pipeline the page shows it as a failure nobody explains.

What it must never do: change a verdict, change an uptime figure, or claim a
cause. It says a release is going out, and afterwards which incidents began
in one.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from status_service import db, ingest, subscribers
from status_service.aggregator import incident_events, latest_per_service, overall_status, uptime_windows
from status_service.config import reset_settings
from status_service.main import app
from status_service.snapshot import build_snapshot

RELEASE_SECRET = "release-secret-" + "r" * 32
PLATFORM_SECRET = "platform-secret-" + "p" * 32
VANTAGE_SECRET = "vantage-secret-" + "v" * 32
ADMIN_SECRET = "test-secret-deadbeef"                      # conftest's ADMIN_HMAC_SECRET
BUILD = "prod-1a2b3c4"

NOTICE = "A new version of YourBot is being released right now."
BEGAN_DURING = "Began while a new version was being released."
BEGAN_AFTER = "Began just after a new version was released."


@pytest.fixture(autouse=True)
def _secrets(monkeypatch):
    monkeypatch.setenv("INGEST_RELEASE_SECRET", RELEASE_SECRET)
    monkeypatch.setenv("INGEST_PLATFORM_SECRET", PLATFORM_SECRET)
    monkeypatch.setenv("INGEST_VANTAGE_SECRET", VANTAGE_SECRET)
    reset_settings()
    yield
    reset_settings()


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _post(client, payload: dict, secret: str = RELEASE_SECRET, ts: int | None = None):
    body = json.dumps(payload).encode()
    return client.post("/ingest/release", content=body,
                       headers={"Content-Type": "application/json", **ingest.sign(secret, body, ts)})


def _start(version: str = BUILD, minutes: int | None = None, ts: int | None = None) -> dict:
    payload = {"action": "start", "version": version}
    if minutes is not None:
        payload["expected_minutes"] = minutes
    return ingest.store_release_event(payload, ts if ts is not None else int(time.time()))


def _finish(version: str = BUILD, result: str = "done", ts: int | None = None) -> dict:
    return ingest.store_release_event({"action": "finish", "version": version, "result": result},
                                      ts if ts is not None else int(time.time()))


def _releases() -> list[dict]:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM releases ORDER BY id").fetchall()]


def _release_row(started: datetime, ended: datetime | None, expires: datetime | None = None, version: str = BUILD) -> None:
    with db.connect() as conn:
        conn.execute("INSERT INTO releases(version, started_at, expires_at, ended_at, result) VALUES (?,?,?,?,?)",
                     (version, _iso(started), _iso(expires or (started + timedelta(minutes=30))),
                      _iso(ended) if ended else None, "done" if ended else None))


def _incident(service: str, started: datetime, minutes: int | None = 6) -> None:
    with db.connect() as conn:
        if minutes is None:
            conn.execute("INSERT INTO incidents(service_name, started_at, resolved) VALUES (?,?,0)",
                         (service, _iso(started)))
        else:
            conn.execute(
                "INSERT INTO incidents(service_name, started_at, ended_at, duration_min, resolved) VALUES (?,?,?,?,1)",
                (service, _iso(started), _iso(started + timedelta(minutes=minutes)), minutes))


def _checks(service: str, statuses: list[str]) -> None:
    """One check a minute, the last one just now."""
    now = _now()
    with db.connect() as conn:
        for index, status in enumerate(statuses):       # oldest first, as the scheduler writes them
            conn.execute(
                "INSERT INTO probe_results(service_name, status, response_ms, source, checked_at) VALUES (?,?,?,?,?)",
                (service, status, 40, "proxy", _iso(now - timedelta(minutes=len(statuses) - 1 - index))))


# ── Who may say a release is going out ────────────────────────────────────

def test_the_route_does_not_exist_until_its_secret_is_set(monkeypatch):
    monkeypatch.setenv("INGEST_RELEASE_SECRET", "")
    reset_settings()
    with TestClient(app) as client:
        assert _post(client, {"action": "start", "version": BUILD}).status_code == 404
    monkeypatch.setenv("INGEST_RELEASE_SECRET", "too-short")
    reset_settings()
    with TestClient(app) as client:
        assert _post(client, {"action": "start", "version": BUILD}, secret="too-short").status_code == 404
    assert _releases() == []


@pytest.mark.parametrize("secret", [PLATFORM_SECRET, VANTAGE_SECRET, ADMIN_SECRET, "wrong-" + "x" * 40])
def test_only_the_release_secret_can_speak_for_the_pipeline(secret):
    with TestClient(app) as client:
        assert _post(client, {"action": "start", "version": BUILD}, secret=secret).status_code == 401
    assert _releases() == []


def test_the_release_secret_can_do_nothing_else():
    """It lives on the machine that deploys. A leak of it must not be able to
    post an announcement or speak for the platform's health."""
    body = json.dumps({"type": "incident", "severity": "critical", "title": "x", "body": "y"}).encode()
    headers = {"Content-Type": "application/json", **ingest.sign(RELEASE_SECRET, body)}
    with TestClient(app) as client:
        assert client.post("/admin/announce", content=body, headers=headers).status_code == 401
        assert client.post("/ingest/platform", content=body, headers=headers).status_code == 401
        assert client.post("/ingest/vantage", content=body, headers=headers).status_code == 401


@pytest.mark.parametrize(("payload", "status"), [
    ({"action": "begin", "version": BUILD}, 422),
    ({"version": BUILD}, 422),
    ({"action": "start"}, 422),
    ({"action": "start", "version": ""}, 422),
    ({"action": "start", "version": "has space"}, 422),
    ({"action": "start", "version": "<b>x</b>"}, 422),
    ({"action": "start", "version": "v" * 65}, 422),
    ({"action": "finish", "version": BUILD, "result": "exploded"}, 422),
])
def test_a_malformed_event_is_refused(payload, status):
    with TestClient(app) as client:
        assert _post(client, payload).status_code == status
    assert _releases() == []


def test_an_unsigned_or_old_event_is_refused():
    body = json.dumps({"action": "start", "version": BUILD}).encode()
    with TestClient(app) as client:
        assert client.post("/ingest/release", content=body).status_code == 401
        old = int(time.time()) - ingest.REPLAY_WINDOW_SECONDS - 5
        assert _post(client, {"action": "start", "version": BUILD}, ts=old).status_code == 401
        assert client.post("/ingest/release", content=b"not json",
                           headers=ingest.sign(RELEASE_SECRET, b"not json")).status_code == 422
    assert _releases() == []


# ── The notice ────────────────────────────────────────────────────────────

def test_a_release_puts_a_notice_up_and_finishing_takes_it_down():
    with TestClient(app) as client:
        before = client.get("/").text
        assert NOTICE not in before

        started = _post(client, {"action": "start", "version": BUILD, "expected_minutes": 25})
        assert started.status_code == 200 and started.json()["ok"] is True

        page, live, api = client.get("/").text, client.get("/live").text, client.get("/api").json()
        assert NOTICE in page and NOTICE in live
        assert "Bots can go offline for a few minutes while they restart." in page
        assert 'class="notices"' in page                       # the region is shown, not collapsed
        assert api["meta"]["release_in_progress"] is True
        assert api["meta"]["release_started_at"] == _releases()[0]["started_at"]

        finished = _post(client, {"action": "finish", "version": BUILD, "result": "done"})
        assert finished.status_code == 200

        page, api = client.get("/").text, client.get("/api").json()
        assert NOTICE not in page and NOTICE not in client.get("/live").text
        assert api["meta"]["release_in_progress"] is False
        assert api["meta"]["release_started_at"] is None
    assert _releases()[0]["result"] == "done"


def test_the_notice_shows_at_once_even_with_the_page_cache_on(monkeypatch):
    """Checks land once a minute, so the page is cached for a few seconds. A
    release must not wait for that: by the time the first restart happens the
    notice has to be there."""
    monkeypatch.setenv("API_CACHE_SECONDS", "300")
    reset_settings()
    with TestClient(app) as client:
        assert NOTICE not in client.get("/live").text         # fills the cache
        assert client.get("/api").json()["meta"]["release_in_progress"] is False
        _post(client, {"action": "start", "version": BUILD})
        assert NOTICE in client.get("/live").text
        assert client.get("/api").json()["meta"]["release_in_progress"] is True
        _post(client, {"action": "finish", "version": BUILD})
        assert NOTICE not in client.get("/live").text
        assert client.get("/api").json()["meta"]["release_in_progress"] is False


def test_the_page_never_names_the_build():
    """The pipeline's tag is not the version in the patch notes. Showing it
    would only make people ask which version they are on."""
    _start("prod-zz99secret")
    _incident("Gateway", _now() - timedelta(minutes=1), minutes=None)
    # Not even in what the templates are handed: a field that is there gets printed one day.
    assert build_snapshot()["release"] == {"started_at": _releases()[0]["started_at"]}
    with TestClient(app) as client:
        for path in ("/", "/live", "/history", "/api", "/api/incidents", "/feed.xml"):
            assert "prod-zz99secret" not in client.get(path).text, path


def test_a_pipeline_that_dies_cannot_leave_the_notice_up():
    _start(minutes=5)
    assert ingest.current_release() is not None
    with db.connect() as conn:                      # five minutes pass without a "finish"
        conn.execute("UPDATE releases SET expires_at=?", (_iso(_now() - timedelta(seconds=1)),))
    assert ingest.current_release() is None
    assert build_snapshot()["release"] is None
    with TestClient(app) as client:
        assert NOTICE not in client.get("/").text


@pytest.mark.parametrize(("asked", "kept"), [
    (None, ingest.RELEASE_DEFAULT_MINUTES), (0, ingest.RELEASE_MIN_MINUTES), (-30, ingest.RELEASE_MIN_MINUTES),
    (25, 25), (100000, ingest.RELEASE_MAX_MINUTES), (True, ingest.RELEASE_DEFAULT_MINUTES),
    ("soon", ingest.RELEASE_DEFAULT_MINUTES),
])
def test_how_long_the_notice_may_stay_up_is_bounded(asked, kept):
    payload = {"action": "start", "version": BUILD}
    if asked is not None:
        payload["expected_minutes"] = asked
    ingest.store_release_event(payload, int(time.time()))
    row = _releases()[0]
    lasts = (ingest._parse(row["expires_at"]) - ingest._parse(row["started_at"])).total_seconds() / 60
    assert round(lasts) == kept


def test_starting_twice_is_one_release_and_never_shortens_it():
    first = _start(minutes=60)
    again = _start(minutes=5)
    rows = _releases()
    assert len(rows) == 1 and again["repeated"] is True and again["release"] == first["release"]
    lasts = (ingest._parse(rows[0]["expires_at"]) - ingest._parse(rows[0]["started_at"])).total_seconds() / 60
    assert round(lasts) == 60


def test_a_new_release_replaces_the_one_before():
    _start("prod-aaaaaaa")
    _start("prod-bbbbbbb")
    rows = _releases()
    assert [(r["version"], r["result"]) for r in rows] == [("prod-aaaaaaa", "replaced"), ("prod-bbbbbbb", None)]
    assert rows[0]["ended_at"] is not None
    assert ingest.current_release()["version"] == "prod-bbbbbbb"


def test_finishing_a_release_nobody_started_changes_nothing():
    stored = _finish("prod-unknown")
    assert stored["release"] is None
    assert _releases() == []
    _start("prod-aaaaaaa")
    assert _finish("prod-bbbbbbb")["release"] is None       # someone else's finish does not end this one
    assert ingest.current_release()["version"] == "prod-aaaaaaa"


def test_a_replayed_start_cannot_bring_the_notice_back():
    """A captured "start", sent again once the release has ended."""
    body = json.dumps({"action": "start", "version": BUILD}).encode()
    captured = {"Content-Type": "application/json", **ingest.sign(RELEASE_SECRET, body)}
    with TestClient(app) as client:
        assert client.post("/ingest/release", content=body, headers=captured).status_code == 200
        assert _post(client, {"action": "finish", "version": BUILD}).status_code == 200
        assert ingest.current_release() is None

        again = client.post("/ingest/release", content=body, headers=captured)      # byte for byte the same
        assert again.status_code == 409
        assert ingest.current_release() is None and len(_releases()) == 1
        assert NOTICE not in client.get("/").text

        # The next release is a different request (signed at another second) and is heard.
        assert _post(client, {"action": "start", "version": BUILD}, ts=int(time.time()) + 3).status_code == 200
    assert ingest.current_release() is not None and len(_releases()) == 2


def test_two_machines_whose_clocks_disagree_are_both_heard():
    """The build sends around the Cloud Run deploys, then the deploy script
    sends around the VM half, seconds later, from another machine. Ordering
    events by the sender's clock would refuse the second machine whenever its
    clock runs behind."""
    now = int(time.time())
    with TestClient(app) as client:
        assert _post(client, {"action": "start", "version": BUILD}, ts=now + 20).status_code == 200
        assert _post(client, {"action": "finish", "version": BUILD}, ts=now + 21).status_code == 200
        # half a minute "earlier" by its own clock, but really a moment later
        assert _post(client, {"action": "start", "version": BUILD}, ts=now - 10).status_code == 200
        assert NOTICE in client.get("/").text
        assert _post(client, {"action": "finish", "version": BUILD}, ts=now - 9).status_code == 200
    assert [r["result"] for r in _releases()] == ["done", "done"]


def test_requests_are_only_remembered_for_as_long_as_they_could_be_replayed():
    now = int(time.time())
    with TestClient(app) as client:
        for i in range(5):
            assert _post(client, {"action": "start", "version": BUILD}, ts=now + i).status_code == 200
        seen = json.loads(db.kv_get(ingest.RELEASE_SEEN_KEY))
        assert len(seen) == 5
        # time passes: none of those signatures would pass the timestamp check any more
        db.kv_set(ingest.RELEASE_SEEN_KEY, json.dumps(
            {sig: at - ingest.REPLAY_WINDOW_SECONDS - 120 for sig, at in seen.items()}))
        assert _post(client, {"action": "finish", "version": BUILD}, ts=now + 9).status_code == 200
    assert len(json.loads(db.kv_get(ingest.RELEASE_SEEN_KEY))) == 1


@pytest.mark.parametrize("damaged", ["not json", "[]", '{"abc": "yesterday"}', ""])
def test_a_damaged_memory_of_requests_does_not_silence_the_pipeline(damaged):
    db.kv_set(ingest.RELEASE_SEEN_KEY, damaged)
    with TestClient(app) as client:
        assert _post(client, {"action": "start", "version": BUILD}).status_code == 200
    assert ingest.current_release() is not None


def test_an_event_that_fails_half_way_changes_nothing():
    """Starting a release closes the one before it and then adds the new one.
    If the second step fails, the first must not stay done."""
    _start("prod-aaaaaaa")
    before = _releases()
    with db.connect() as conn:                      # the next insert will fail
        conn.execute("CREATE TRIGGER no_room BEFORE INSERT ON releases "
                     "BEGIN SELECT RAISE(ABORT, 'database or disk is full'); END")
    with pytest.raises(sqlite3.DatabaseError):
        _start("prod-bbbbbbb")
    with db.connect() as conn:
        conn.execute("DROP TRIGGER no_room")
    assert _releases() == before
    assert ingest.current_release()["version"] == "prod-aaaaaaa"


def test_a_release_that_overran_gets_its_real_end():
    _start(minutes=5)
    with db.connect() as conn:                      # it took longer than it said it would
        conn.execute("UPDATE releases SET started_at=?, expires_at=?",
                     (_iso(_now() - timedelta(minutes=40)), _iso(_now() - timedelta(minutes=35))))
    assert ingest.current_release() is None          # the notice went down when its time ran out
    assert _finish()["release"] is not None
    row = _releases()[0]
    assert row["result"] == "done"
    assert (_now() - ingest._parse(row["ended_at"])).total_seconds() < 5


def test_a_failed_release_is_kept_as_failed():
    _start()
    _finish(result="failed")
    assert _releases()[0]["result"] == "failed"
    assert ingest.current_release() is None


def test_a_release_left_open_past_its_time_is_closed_by_the_next_one():
    _start("prod-aaaaaaa", minutes=5)
    expired_at = _iso(_now() - timedelta(minutes=10))
    with db.connect() as conn:
        conn.execute("UPDATE releases SET started_at=?, expires_at=?",
                     (_iso(_now() - timedelta(minutes=15)), expired_at))
    _start("prod-bbbbbbb")
    first = _releases()[0]
    assert (first["result"], first["ended_at"]) == ("expired", expired_at)


# ── Incidents that began in a release ─────────────────────────────────────

@pytest.mark.parametrize(("began_after_start_min", "expected"), [
    (-2, None),            # already failing before the release began
    (0, "during"),
    (10, "during"),
    (20, "during"),        # the minute it ended
    (23, "after"),         # the checker needs a moment to notice
    (25, "after"),
    (26, None),
    (120, None),
])
def test_when_an_incident_began_relative_to_a_release(began_after_start_min, expected):
    start = _now() - timedelta(hours=3)
    windows = [(start, start + timedelta(minutes=20))]
    assert ingest.release_at(start + timedelta(minutes=began_after_start_min), windows) == expected


def test_during_one_release_wins_over_after_another():
    start = _now() - timedelta(hours=3)
    windows = [(start, start + timedelta(minutes=10)), (start + timedelta(minutes=12), start + timedelta(minutes=30))]
    assert ingest.release_at(start + timedelta(minutes=13), windows) == "during"


def test_an_incident_that_began_during_a_release_says_so_everywhere():
    began = _now() - timedelta(hours=2)
    _release_row(began - timedelta(minutes=4), began + timedelta(minutes=10))
    _incident("Gateway", began)
    _incident("Bot", began + timedelta(minutes=1))

    events = incident_events(days=7)
    assert len(events) == 1 and events[0]["release"] == "during"
    with TestClient(app) as client:
        assert BEGAN_DURING in client.get("/").text
        assert BEGAN_DURING in client.get("/live").text
        assert BEGAN_DURING in client.get("/history").text
        assert client.get("/api/incidents").json()["events"][0]["release"] == "during"


def test_an_incident_just_after_a_release_says_that_instead():
    began = _now() - timedelta(hours=2)
    _release_row(began - timedelta(minutes=20), began - timedelta(minutes=3))
    _incident("Bot Worker", began)
    assert incident_events(days=7)[0]["release"] == "after"
    with TestClient(app) as client:
        page = client.get("/").text
    assert BEGAN_AFTER in page and BEGAN_DURING not in page


def test_an_incident_outside_any_release_is_not_tied_to_one():
    began = _now() - timedelta(hours=2)
    _release_row(began - timedelta(hours=5), began - timedelta(hours=4, minutes=40))
    _incident("Gateway", began)
    assert incident_events(days=7)[0]["release"] is None
    with TestClient(app) as client:
        page = client.get("/").text
    assert "event-release" not in page
    assert BEGAN_DURING not in page and BEGAN_AFTER not in page


def test_an_incident_starting_now_is_tied_to_the_release_still_going_out():
    _start()
    with db.connect() as conn:                      # the release began two minutes ago
        conn.execute("UPDATE releases SET started_at=?", (_iso(_now() - timedelta(minutes=2)),))
    _incident("Gateway", _now() - timedelta(seconds=20), minutes=None)
    assert incident_events(days=7)[0]["release"] == "during"


def test_an_incident_already_running_when_the_release_began_is_not_tied_to_it():
    _incident("Gateway", _now() - timedelta(minutes=10), minutes=None)
    _start()
    assert incident_events(days=7)[0]["release"] is None


def test_a_release_whose_end_was_never_reported_ends_when_its_time_ran_out():
    started = _now() - timedelta(hours=3)
    _release_row(started, None, expires=started + timedelta(minutes=30))
    _incident("Gateway", started + timedelta(minutes=29))
    _incident("Cache", started + timedelta(minutes=90))        # an hour after the notice had gone
    events = {e["services"][0]: e["release"] for e in incident_events(days=7)}
    assert events == {"Gateway": "during", "Cache": None}


def test_a_written_cause_and_the_release_are_shown_side_by_side():
    began = _now() - timedelta(hours=2)
    _release_row(began - timedelta(minutes=4), began + timedelta(minutes=10))
    _incident("Gateway", began)
    with db.connect() as conn:
        conn.execute("UPDATE incidents SET cause=?, cause_at=?", ("A bad config value.", _iso(_now())))
    with TestClient(app) as client:
        page = client.get("/").text
    assert BEGAN_DURING in page and "A bad config value." in page and "Why this happened" in page


# ── What a release must never do ──────────────────────────────────────────

def test_a_release_changes_no_verdict_and_no_uptime_figure():
    _checks("Public Site", ["operational"] * 10)
    _checks("Gateway", ["operational"] * 6 + ["down"] * 4)

    def measured() -> tuple:
        windows = uptime_windows()
        snap = build_snapshot()
        return (overall_status(latest_per_service()), snap["headline"], windows["Gateway"]["24h"],
                [(c["key"], c["status"]) for c in snap["components"]])

    before = measured()
    _start()
    during = measured()
    _finish()
    after = measured()

    assert before == during == after
    assert before[0] == "outage" and before[2]["failed"] == 4      # the failure is real and stays counted


def test_releases_are_never_sent_to_subscribers_or_put_in_the_feed(monkeypatch):
    async def must_not_be_called(*args, **kwargs):
        raise AssertionError("a release was broadcast to subscribers")

    monkeypatch.setattr(subscribers, "broadcast_announcement", must_not_be_called)
    with TestClient(app) as client:
        assert _post(client, {"action": "start", "version": BUILD}).status_code == 200
        assert _post(client, {"action": "finish", "version": BUILD}).status_code == 200
        feed = client.get("/feed.xml").text
    assert "release" not in feed.lower()
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM announcements").fetchone()["n"] == 0


def test_the_page_still_renders_when_the_releases_cannot_be_read(monkeypatch):
    _incident("Gateway", _now() - timedelta(hours=1))

    def broken(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(ingest, "current_release", broken)
    monkeypatch.setattr(ingest, "release_windows", broken)
    assert incident_events(days=7)[0]["release"] is None
    with TestClient(app) as client:
        response = client.get("/")
    assert response.status_code == 200 and NOTICE not in response.text


def test_rolling_back_stays_a_plain_image_swap():
    """A new table needs no schema version bump. The release before this one
    refuses to start on a database with a higher version, and it never reads
    a table it does not know, so leaving the version alone keeps a rollback
    free of any manual step."""
    assert db.SCHEMA_VERSION == 7
    with db.connect() as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()["version"] == 7
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(releases)").fetchall()}
    assert columns == {"id", "version", "started_at", "expires_at", "ended_at", "result"}


def test_a_fresh_start_of_the_monitoring_keeps_a_release_notice():
    """Wiping collected history is not a reason to take down a notice the
    pipeline put up a minute ago."""
    _start()
    db.reset_monitoring_data()
    assert ingest.current_release() is not None


def test_the_words_follow_the_house_rules():
    for text in (NOTICE, BEGAN_DURING, BEGAN_AFTER, "Bots can go offline for a few minutes while they restart."):
        assert "—" not in text and "–" not in text
    _start()
    with TestClient(app) as client:
        page = client.get("/").text
    notice = page[page.index("notice notice-info"):page.index("</div>", page.index("notice notice-info"))]
    assert "—" not in notice and "–" not in notice
