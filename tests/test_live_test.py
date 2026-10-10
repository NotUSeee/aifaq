"""The live test: a message is posted in a private Discord server and the bot
has to react to it.

What it must get right:
  * a reaction is the bot answering; nothing else counts
  * one unanswered message is not an outage: it is confirmed with a second
  * one late answer is not a slow bot: that is confirmed with a second too
  * when the test could not be run, that is "no data", never "down"
  * the webhook's address is a secret and appears nowhere
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from status_service import db
from status_service.aggregator import incident_events, latest_per_service, overall_status
from status_service.config import get_settings, reset_settings
from status_service.main import app
from status_service.probes import ProbeResult, live_test
from status_service.scheduler import DISCORD_TROUBLE_ERROR, Scheduler
from status_service.snapshot import build_snapshot
from tests.test_measurement import BASE, CONTROLS, DISCORD, DISCORD_OK, _healthy, _rows, _statuses, sched  # noqa: F401

REAL_POLL_AT = live_test.POLL_AT            # read before the fixture below replaces it
REAL_LIMIT_SLACK = live_test.LIMIT_SLACK_SECONDS
TOKEN = "s3cretTokenValue_abcdefghijklmnopqrstuvwxyz-0123456789"
WEBHOOK = f"https://discord.com/api/webhooks/123456789012345678/{TOKEN}"
CHECK = "✅"
NAME = live_test.SERVICE_NAME


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    """The real schedule waits half a second between reads. Same order, no waiting."""
    monkeypatch.setattr(live_test, "POLL_AT", (0.0, 0.001, 0.002, 0.003, 0.004, 0.005))
    monkeypatch.setattr(live_test, "RETRY_PAUSE_SECONDS", 0.0)
    monkeypatch.setattr(live_test, "LIMIT_SLACK_SECONDS", 0.0)     # these tests work in hundredths of a second


class FakeDiscord:
    """The three webhook calls the test makes, with a bot that reacts (or not)."""

    def __init__(self, respx_mock, *, react_on_read: int | None | list = 1, emoji: str = CHECK,
                 post: list | None = None, read: list | None = None, delete: list | None = None):
        self.react_plan = react_on_read if isinstance(react_on_read, list) else None
        self.react_on_read = None if self.react_plan is not None else react_on_read
        self.emoji = emoji
        self.post_plan = list(post or [])      # responses or exceptions to serve first
        self.read_plan = list(read or [])
        self.delete_plan = list(delete or [])
        self.posted: list[dict] = []
        self.reads: dict[str, int] = {}
        self.deleted: list[str] = []
        self.post_params: list[dict] = []
        respx_mock.post(WEBHOOK).mock(side_effect=self._post)
        respx_mock.get(url__regex=rf"^{WEBHOOK}/messages/\d+$").mock(side_effect=self._read)
        respx_mock.delete(url__regex=rf"^{WEBHOOK}/messages/\d+$").mock(side_effect=self._delete)

    def _post(self, request: httpx.Request):
        if self.post_plan:
            planned = self.post_plan.pop(0)
            if isinstance(planned, Exception):
                raise planned
            return planned
        self.post_params.append(dict(request.url.params))
        body = json.loads(request.content)
        self.posted.append(body)
        return httpx.Response(200, json={"id": str(1000 + len(self.posted)), "content": body.get("content")})

    def _reacts_on(self, message_id: str) -> int | None:
        if self.react_plan is not None:
            index = int(message_id) - 1001
            return self.react_plan[index] if index < len(self.react_plan) else None
        return self.react_on_read

    def _read(self, request: httpx.Request):
        if self.read_plan:
            planned = self.read_plan.pop(0)
            if isinstance(planned, Exception):
                raise planned
            return planned
        message_id = request.url.path.rsplit("/", 1)[1]
        self.reads[message_id] = self.reads.get(message_id, 0) + 1
        message = {"id": message_id, "content": "Status check", "webhook_id": "123456789012345678"}
        after = self._reacts_on(message_id)
        if after is not None and self.reads[message_id] >= after:
            message["reactions"] = [{"count": 1, "me": False, "emoji": {"id": None, "name": self.emoji}}]
        return httpx.Response(200, json=message)

    def _delete(self, request: httpx.Request):
        if self.delete_plan:
            planned = self.delete_plan.pop(0)
            if isinstance(planned, Exception):
                raise planned
            return planned
        self.deleted.append(request.url.path.rsplit("/", 1)[1])
        return httpx.Response(204)


async def _run(**kwargs) -> ProbeResult:
    async with httpx.AsyncClient() as client:
        return await live_test.probe_live_test(client, WEBHOOK, emoji=CHECK, **{"deadline": 1.0, **kwargs})


# ── The address ───────────────────────────────────────────────────────────

@pytest.mark.parametrize(("url", "accepted"), [
    (WEBHOOK, True),
    (WEBHOOK + "/", True),
    (WEBHOOK + "?wait=true", True),
    (WEBHOOK.replace("/api/", "/api/v10/"), True),
    (WEBHOOK.replace("discord.com", "discordapp.com"), True),
    ("  " + WEBHOOK + "  ", True),
    (WEBHOOK.replace("https://", "http://"), False),
    (WEBHOOK.replace("discord.com", "discord.com.evil.example"), False),
    (WEBHOOK.replace("discord.com", "evil.example"), False),
    (WEBHOOK.replace("discord.com", "discord.com:8443"), False),
    (WEBHOOK + "/slack", False),
    # a stand-in for Discord on this machine itself, for a local rehearsal
    (f"http://127.0.0.1:8770/api/webhooks/123456789012345678/{TOKEN}", True),
    (f"http://localhost:8770/api/webhooks/123456789012345678/{TOKEN}", True),
    (f"http://127.0.0.1/api/webhooks/123456789012345678/{TOKEN}", False),           # no port: not a rehearsal
    (f"http://127.0.0.1.evil.example:8770/api/webhooks/123456789012345678/{TOKEN}", False),
    (f"http://10.0.0.5:8770/api/webhooks/123456789012345678/{TOKEN}", False),        # another machine
    (f"http://user@127.0.0.1:8770/api/webhooks/123456789012345678/{TOKEN}", False),
    (f"https://discord.com:99999/api/webhooks/123456789012345678/{TOKEN}", False),   # not a port at all
    (WEBHOOK + "/messages/1", False),
    ("https://discord.com/api/webhooks/123456789012345678", False),
    ("https://discord.com/channels/1/2", False),
    ("", False),
    (None, False),
])
def test_only_a_discord_webhook_address_is_accepted(url, accepted):
    """A typo must not make this service post a message a minute somewhere else."""
    parsed = live_test.parse_webhook_url(url)
    assert (parsed is not None) is accepted
    if accepted:
        assert parsed.startswith(("https://discord", "http://127.0.0.1:", "http://localhost:"))
        assert "?" not in parsed and not parsed.endswith("/") and "@" not in parsed


# ── One run ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_reaction_is_the_bot_answering(respx_mock):
    discord = FakeDiscord(respx_mock, react_on_read=2)
    result = await _run()

    assert (result.service_name, result.status, result.source) == (NAME, "operational", "live_test")
    assert result.response_ms is not None and result.response_ms >= 0
    assert len(discord.posted) == 1
    assert discord.post_params[0] == {"wait": "true"}             # we need the message id back
    sent = discord.posted[0]
    assert sent["content"].startswith("Status check ")
    assert sent["allowed_mentions"] == {"parse": []}              # it can never ping anyone
    assert sent["flags"] == 4096                                  # and never notifies
    assert discord.deleted == ["1001"]                            # the channel is left as it was


# Late answers. The reads go out at 0, 0.05 and 0.1 s and "late" starts after
# 0.02 s, so a reaction that is there on the third read is late and one that is
# there on the first is in good time.
LATE = {"slow_after": 0.02}


@pytest.fixture
def three_reads(monkeypatch):
    monkeypatch.setattr(live_test, "POLL_AT", (0.0, 0.05, 0.1))


@pytest.mark.asyncio
async def test_two_late_answers_are_a_slow_bot(respx_mock, three_reads):
    discord = FakeDiscord(respx_mock, react_on_read=3)            # every message: late
    result = await _run(**LATE)
    assert result.status == "degraded"
    assert result.response_ms >= 90
    assert result.extra == {"attempts": 2}
    assert len(discord.posted) == 2 and discord.deleted == ["1001", "1002"]


@pytest.mark.asyncio
async def test_one_late_answer_is_not_a_slow_bot(respx_mock, three_reads):
    """In production the answer takes anything from 0.6 to 5 seconds. With the
    first release one answer above the limit put "Degraded performance" on the
    public page for a minute, twice in seven minutes."""
    discord = FakeDiscord(respx_mock, react_on_read=[3, 1])       # late, then in good time
    result = await _run(**LATE)
    assert result.status == "operational"
    assert result.response_ms < 90, "the time shown is the answer that was believed"
    assert result.extra == {"attempts": 2}
    assert len(discord.posted) == 2 and discord.deleted == ["1001", "1002"]


@pytest.mark.asyncio
async def test_the_time_shown_for_a_slow_bot_is_its_better_answer(respx_mock, three_reads):
    FakeDiscord(respx_mock, react_on_read=[3, 2])                 # seen at 0.1 s, then at 0.05 s: both late
    result = await _run(**LATE)
    assert result.status == "degraded"
    assert 40 <= result.response_ms < 90


@pytest.mark.parametrize("plan", [[3, None], [None, 3]])
@pytest.mark.asyncio
async def test_a_late_answer_and_a_missing_one_are_a_slow_bot(respx_mock, three_reads, plan):
    FakeDiscord(respx_mock, react_on_read=plan)
    result = await _run(**LATE)
    assert result.status == "degraded"
    assert result.response_ms >= 90 and result.extra == {"attempts": 2}


@pytest.mark.asyncio
async def test_a_late_answer_that_could_not_be_confirmed_is_still_an_answer(respx_mock, three_reads):
    """First message: answered late. Second: Discord would not take it. The
    bot did answer, and nothing confirmed that it is slow."""

    class Plan(FakeDiscord):
        def _post(self, request):
            if len(self.posted) == 1:
                return httpx.Response(503)
            return super()._post(request)

    Plan(respx_mock, react_on_read=3)
    result = await _run(**LATE)
    assert result.status == "operational"
    assert result.response_ms >= 90 and result.extra == {"attempts": 2}


@pytest.mark.asyncio
async def test_with_a_single_attempt_the_first_result_stands(respx_mock, three_reads):
    discord = FakeDiscord(respx_mock, react_on_read=3)
    result = await _run(attempts=1, **LATE)
    assert result.status == "degraded" and result.extra == {"attempts": 1}
    assert len(discord.posted) == 1


@pytest.mark.asyncio
async def test_late_is_judged_by_when_we_looked_not_by_how_long_the_look_took(respx_mock, monkeypatch):
    """The reaction is there on the very first read, but Discord takes 80 ms to
    answer that read. The bot was not late: our question was slow."""
    import time as _time

    monkeypatch.setattr(live_test, "POLL_AT", (0.0, 0.05, 0.1))

    class SlowReads(FakeDiscord):
        def _read(self, request):
            _time.sleep(0.08)
            return super()._read(request)

    discord = SlowReads(respx_mock, react_on_read=1)
    result = await _run(slow_after=0.03)
    assert result.status == "operational"
    assert result.response_ms >= 70, "the time shown is still when the answer was seen"
    assert len(discord.posted) == 1


@pytest.mark.asyncio
async def test_a_look_at_the_limit_counts_as_in_time(respx_mock, monkeypatch):
    """Reads go out on a timer and a timer is never exactly on time. A read
    planned AT the limit must not turn into "late" because it left 3 ms after
    it, or the verdict would flip on scheduling noise."""
    monkeypatch.setattr(live_test, "POLL_AT", (0.0, 0.05))
    monkeypatch.setattr(live_test, "LIMIT_SLACK_SECONDS", 0.25)
    discord = FakeDiscord(respx_mock, react_on_read=2)            # seen by the read planned at 0.05 s
    result = await _run(slow_after=0.05)
    assert result.status == "operational" and len(discord.posted) == 1


def test_the_real_schedule_looks_every_second_up_to_ten():
    """So that a limit of ten seconds is measured in whole seconds. The first
    schedule went 5, 6.5, 8, 10: an answer at 8.1 s was only seen at 10."""
    upto = [t for t in REAL_POLL_AT if t <= 10.0]
    assert upto[-1] == 10.0 and upto[0] <= 0.5
    assert max(b - a for a, b in zip(upto, upto[1:])) <= 1.0
    assert list(REAL_POLL_AT) == sorted(REAL_POLL_AT)
    assert REAL_POLL_AT[-1] >= 20.0, "the reads must reach the deadline"
    assert 0.1 <= REAL_LIMIT_SLACK <= 0.5, "enough for a timer, far too little to hide a late answer"


def test_late_starts_at_ten_seconds_unless_set_otherwise(monkeypatch):
    monkeypatch.delenv("LIVE_TEST_SLOW_SECONDS", raising=False)
    reset_settings()
    assert get_settings().live_test_slow_seconds == 10.0
    monkeypatch.setenv("LIVE_TEST_SLOW_SECONDS", "7.5")
    reset_settings()
    assert get_settings().live_test_slow_seconds == 7.5
    reset_settings()


@pytest.mark.asyncio
async def test_no_reaction_twice_is_an_outage(respx_mock):
    discord = FakeDiscord(respx_mock, react_on_read=None)
    result = await _run()

    assert result.status == "down"
    assert result.error == "no reaction within 1 s"
    assert result.response_ms is None
    assert len(discord.posted) == 2                               # believed only when confirmed
    assert discord.deleted == ["1001", "1002"]                    # both test messages taken away again
    assert result.extra == {"attempts": 2}


@pytest.mark.asyncio
async def test_one_unanswered_message_is_not_an_outage(respx_mock):
    discord = FakeDiscord(respx_mock, react_on_read=[None, 1])    # first one lost, second answered
    result = await _run()
    assert result.status == "operational"
    assert result.extra == {"attempts": 2}
    assert len(discord.posted) == 2 and discord.deleted == ["1001", "1002"]


@pytest.mark.asyncio
async def test_a_different_reaction_is_not_the_answer(respx_mock):
    FakeDiscord(respx_mock, react_on_read=1, emoji="\U0001F44D")  # a thumbs up from someone
    assert (await _run()).status == "down"


@pytest.mark.parametrize("first", [
    httpx.Response(500), httpx.Response(429, json={"retry_after": 2}), httpx.Response(400),
    httpx.ConnectError("no route"), httpx.ReadTimeout("slow"),
    httpx.Response(200, json={"no": "id"}), httpx.Response(200, text="not json"),
    httpx.Response(200, json={"id": "not-a-number"}),
])
@pytest.mark.asyncio
async def test_a_test_that_could_not_be_posted_is_no_data_never_down(respx_mock, first):
    """Discord refusing our message says nothing about the bot."""
    discord = FakeDiscord(respx_mock, post=[first, first])
    result = await _run()
    assert result.status == "unknown"
    assert result.error.startswith("could not post the test message")
    assert discord.deleted == []


@pytest.mark.asyncio
async def test_a_deleted_webhook_is_no_data_and_is_reported_in_the_log(respx_mock, caplog):
    FakeDiscord(respx_mock, post=[httpx.Response(404, json={"message": "Unknown Webhook", "code": 10015})] * 2)
    with caplog.at_level(logging.DEBUG):
        result = await _run()
    assert result.status == "unknown" and "HTTP 404" in result.error
    errors = [r for r in caplog.records if r.name == "status_service.live_test" and r.levelno >= logging.ERROR]
    assert errors and "webhook was refused" in errors[0].getMessage()


@pytest.mark.asyncio
async def test_a_message_that_cannot_be_read_back_is_no_data(respx_mock):
    discord = FakeDiscord(respx_mock, read=[httpx.Response(500)] * 20)
    result = await _run()
    assert result.status == "unknown"
    assert result.error == "could not read the test message back"
    assert discord.deleted == ["1001", "1002"]


@pytest.mark.asyncio
async def test_a_message_someone_removed_is_no_data(respx_mock):
    FakeDiscord(respx_mock, read=[httpx.Response(404, json={"message": "Unknown Message"})] * 2)
    result = await _run()
    assert result.status == "unknown"
    assert result.error == "the test message disappeared before it could be read"


@pytest.mark.asyncio
async def test_reads_that_hang_cannot_stretch_a_run_far_past_its_deadline(respx_mock, monkeypatch):
    """Every read has its own timeout. With Discord's API hanging, a run of
    fifteen reads would take minutes, and the page would show "no recent
    test" for all of them."""
    import time as _time

    monkeypatch.setattr(live_test, "POLL_AT", tuple(i / 1000 for i in range(15)))
    monkeypatch.setattr(live_test, "OVERRUN_SLACK_SECONDS", 0.0)
    discord = FakeDiscord(respx_mock, react_on_read=None)
    slow_reads = {"n": 0}

    def slow(request):
        slow_reads["n"] += 1
        _time.sleep(0.06)                       # each read outlasts the whole deadline
        return httpx.Response(500)

    respx_mock.get(url__regex=rf"^{WEBHOOK}/messages/\d+$").mock(side_effect=slow)
    async with httpx.AsyncClient() as client:
        outcome = await live_test.attempt_once(client, WEBHOOK, emoji=CHECK, deadline=0.05)
    assert outcome.answered is None and outcome.detail == "could not read the test message back"
    assert slow_reads["n"] <= 2, f"{slow_reads['n']} reads were started after the deadline had passed"
    assert discord.deleted == ["1001"]


@pytest.mark.asyncio
async def test_being_rate_limited_on_a_read_only_delays_the_answer(respx_mock, monkeypatch):
    monkeypatch.setattr(live_test, "POLL_AT", (0.0, 0.001, 0.002))
    FakeDiscord(respx_mock, react_on_read=1, read=[httpx.Response(429, json={"retry_after": 0.01}),
                                                    httpx.ConnectError("blip")])
    assert (await _run()).status == "operational"


@pytest.mark.asyncio
async def test_an_unanswered_message_that_could_not_be_confirmed_is_no_data(respx_mock):
    """First message: no reaction. Second: Discord would not take it. One
    unconfirmed miss is not enough to call an outage."""

    class Plan(FakeDiscord):
        def _post(self, request):
            if len(self.posted) == 1:
                return httpx.Response(503)
            return super()._post(request)

    Plan(respx_mock, react_on_read=None)
    result = await _run()
    assert result.status == "unknown"
    assert result.error.startswith("could not post the test message")


@pytest.mark.asyncio
async def test_the_message_is_taken_away_even_when_reading_it_raises(respx_mock, monkeypatch):
    discord = FakeDiscord(respx_mock, react_on_read=None)

    def boom(message, emoji):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(live_test, "_reacted", boom)
    async with httpx.AsyncClient() as client:
        with pytest.raises(RuntimeError):
            await live_test.attempt_once(client, WEBHOOK, emoji=CHECK, deadline=1.0)
    assert discord.deleted == ["1001"]


@pytest.mark.parametrize(("message", "answered"), [
    ({"reactions": [{"count": 1, "emoji": {"name": CHECK}}]}, True),
    ({"reactions": [{"count": 3, "emoji": {"name": CHECK}}, {"count": 1, "emoji": {"name": "x"}}]}, True),
    ({"reactions": [{"count": 0, "emoji": {"name": CHECK}}]}, False),
    ({"reactions": [{"count": "many", "emoji": {"name": CHECK}}]}, False),
    ({"reactions": [{"count": 1, "emoji": None}]}, False),
    ({"reactions": ["junk", None]}, False),
    ({"reactions": None}, False),
    ({}, False),
])
def test_what_counts_as_a_reaction(message, answered):
    assert live_test._reacted(message, CHECK) is answered


# ── The address is a secret ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_address_never_reaches_a_log_or_a_stored_error(respx_mock, caplog):
    scenarios = [
        dict(react_on_read=1),
        dict(react_on_read=None),
        dict(post=[httpx.Response(404)] * 2),
        dict(post=[httpx.ConnectError(f"cannot reach {WEBHOOK}")] * 2),
        dict(read=[httpx.ReadTimeout(f"timed out reading {WEBHOOK}/messages/1001")] * 20),
        dict(react_on_read=1, delete=[httpx.ConnectError(f"cannot reach {WEBHOOK}/messages/1001")]),
    ]
    with caplog.at_level(logging.DEBUG):
        for scenario in scenarios:
            respx_mock.reset()
            respx_mock.routes.clear()
            FakeDiscord(respx_mock, **scenario)
            result = await _run()
            assert TOKEN not in (result.error or "") and WEBHOOK not in (result.error or "")
            live_test.remember(result)
            assert TOKEN not in db.kv_get(live_test.LATEST_KEY)
    ours = [r for r in caplog.records if r.name.startswith("status_service")]
    assert ours, "the failing scenarios log something"
    for record in ours:
        assert TOKEN not in record.getMessage()
        assert not record.exc_info, "a traceback can carry the address"


# ── Remembering the last run ──────────────────────────────────────────────

def test_the_last_result_is_kept_only_while_it_is_recent():
    live_test.remember(ProbeResult(service_name=NAME, status="operational", response_ms=840, source="live_test"))
    held = live_test.latest(180)
    assert (held.status, held.response_ms, held.source, held.service_name) == ("operational", 840, "live_test", NAME)

    stale = json.loads(db.kv_get(live_test.LATEST_KEY))
    stale["at"] = (datetime.now(timezone.utc) - timedelta(seconds=181)).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")
    db.kv_set(live_test.LATEST_KEY, json.dumps(stale))
    assert live_test.latest(180) is None


@pytest.mark.parametrize("stored", ["", "not json", "[]", '{"status": "operational"}',
                                    '{"at": "yesterday", "status": "operational"}',
                                    '{"at": "2026-10-09T00:00:00.000Z", "status": "fine"}'])
def test_a_damaged_record_is_no_result(stored):
    if stored:
        db.kv_set(live_test.LATEST_KEY, stored)
    assert live_test.latest(10 ** 9) is None


# ── In the scheduler and on the page ──────────────────────────────────────

@pytest.fixture
def live_sched(monkeypatch, sched):  # noqa: F811
    monkeypatch.setenv("LIVE_TEST_WEBHOOK_URL", WEBHOOK)
    reset_settings()
    scheduler = Scheduler(get_settings())
    return scheduler


def _remember(status: str, ms: int | None = 900, error: str | None = None) -> None:
    live_test.remember(ProbeResult(service_name=NAME, status=status, response_ms=ms, error=error, source="live_test"))


def _answered_once(base: str = WEBHOOK) -> None:
    """The setup has proved itself: the bot answered a test through this webhook."""
    live_test.remember(ProbeResult(service_name=NAME, status="operational", response_ms=700, source="live_test"), base)
    assert live_test.answered_before(base)


@pytest.mark.asyncio
async def test_switched_off_there_is_no_row_and_nothing_on_the_page(sched, respx_mock):  # noqa: F811
    _healthy(respx_mock)
    assert sched.live_test_on is False
    await sched._cycle()
    assert _rows(NAME) == []
    snap = build_snapshot()
    assert snap["live_test"] is None
    with TestClient(app) as client:
        page = client.get("/").text
    assert "Live test" not in page and "test message is posted" not in page


@pytest.mark.asyncio
async def test_a_wrong_address_switches_the_test_off_and_says_so(monkeypatch, sched, caplog):  # noqa: F811
    monkeypatch.setenv("LIVE_TEST_WEBHOOK_URL", f"https://example.com/hook/{TOKEN}")
    reset_settings()
    with caplog.at_level(logging.ERROR, logger="status_service.scheduler"):
        scheduler = Scheduler(get_settings())
    assert scheduler.live_test_on is False
    assert any("is not a Discord webhook address" in r.getMessage() for r in caplog.records)
    assert all(TOKEN not in r.getMessage() for r in caplog.records)
    # Returns at once and posts nothing. Bounded, so a loop that started anyway fails this test instead of hanging it.
    await asyncio.wait_for(scheduler.run_live_test_forever(), timeout=2)


@pytest.mark.asyncio
async def test_an_answered_test_is_a_check_under_commands(live_sched, respx_mock):
    _healthy(respx_mock)
    _remember("operational", ms=640)
    await live_sched._cycle()

    row = _rows(NAME)[-1]
    assert (row["status"], row["source"]) == ("operational", "live_test")
    snap = build_snapshot()
    commands = next(c for c in snap["components"] if c["key"] == "commands")
    check = next(k for k in commands["checks"] if k["name"] == NAME)
    assert (check["label"], check["how"], check["status"], check["advisory"]) == (
        "Live test", "Checked from outside", "operational", False)
    assert snap["overall"] == "operational"
    assert snap["live_test"] == {"interval_text": "Every minute"}
    with TestClient(app) as client:
        page = client.get("/").text
        api = client.get("/api").json()
    assert "Live test" in page
    assert "Every minute a test message is posted in a private Discord server" in page
    assert any(c["name"] == NAME and c["status"] == "operational" for c in api["current"])


@pytest.mark.asyncio
async def test_an_unanswered_test_is_an_incident_people_can_see(live_sched, respx_mock):
    _healthy(respx_mock)
    _answered_once()
    for _ in range(3):
        _remember("down", ms=None, error="no reaction within 20 s")
        await live_sched._cycle()

    assert _statuses()[NAME] == "down"
    assert overall_status(latest_per_service()) == "partial_outage"
    snap = build_snapshot()
    commands = next(c for c in snap["components"] if c["key"] == "commands")
    assert commands["status"] == "partial"                      # the processes run, the answers do not arrive
    events = incident_events(days=1)
    assert events and events[0]["title"] == "Commands and automations disruption"
    assert events[0]["service_labels"] == ["Live test"]


@pytest.mark.asyncio
async def test_an_unanswered_test_is_not_counted_while_discord_is_in_trouble(live_sched, respx_mock):
    _healthy(respx_mock)
    respx_mock.get(DISCORD).mock(return_value=httpx.Response(200, json={
        "components": [{"name": "API", "status": "partial_outage"}, {"name": "Gateway", "status": "operational"}],
        "incidents": [{"name": "Elevated API errors"}]}))
    _remember("down", ms=None, error="no reaction within 20 s")
    await live_sched._cycle()

    row = _rows(NAME)[-1]
    assert (row["status"], row["error"]) == ("unknown", DISCORD_TROUBLE_ERROR)
    snap = build_snapshot()
    check = next(k for c in snap["components"] for k in c["checks"] if k["name"] == NAME)
    assert check["note"] == "Not counted while Discord reports problems of its own."
    assert incident_events(days=1) == []


@pytest.mark.asyncio
async def test_an_answered_test_still_counts_while_discord_is_in_trouble(live_sched, respx_mock):
    _healthy(respx_mock)
    respx_mock.get(DISCORD).mock(return_value=httpx.Response(200, json={
        "components": [{"name": "API", "status": "major_outage"}], "incidents": []}))
    _remember("operational")
    await live_sched._cycle()
    assert _rows(NAME)[-1]["status"] == "operational"


@pytest.mark.asyncio
async def test_discord_trouble_elsewhere_excuses_nothing(live_sched, respx_mock):
    """Voice being down does not stop a bot from reacting to a message."""
    _healthy(respx_mock)                                          # DISCORD_OK has Voice in a major outage
    assert any(c["name"] == "Voice" and c["status"] == "major_outage" for c in DISCORD_OK["components"])
    _answered_once()
    _remember("down", ms=None, error="no reaction within 20 s")
    await live_sched._cycle()
    assert _rows(NAME)[-1]["status"] == "down"


# ── A test that has never worked is a setup, not an outage ────────────────

@pytest.mark.asyncio
async def test_a_test_that_was_never_answered_is_not_published_as_an_outage(live_sched, respx_mock):
    """Switched on, and the bot does not react: a permission is missing in the
    channel, or the platform was given the wrong ids. Until it has worked once
    that is an unfinished setup. Calling it an outage would put a self-made
    incident on the public page the moment someone sets this up."""
    _healthy(respx_mock)
    for _ in range(3):
        _remember("down", ms=None, error="no reaction within 20 s")
        await live_sched._cycle()

    row = _rows(NAME)[-1]
    assert (row["status"], row["error"]) == ("unknown", "never answered")
    snap = build_snapshot()
    check = next(k for c in snap["components"] for k in c["checks"] if k["name"] == NAME)
    assert check["note"] == ("Waiting for the first answered test. "
                             "A missing answer is not counted until the test has worked once.")
    assert snap["overall"] == "operational"
    assert incident_events(days=1) == []
    assert [r["status"] for r in _rows(NAME)].count("down") == 0     # nothing counted toward uptime either


@pytest.mark.asyncio
async def test_once_it_has_been_answered_a_missing_answer_counts(live_sched, respx_mock):
    _healthy(respx_mock)
    _remember("down", ms=None, error="no reaction within 20 s")
    await live_sched._cycle()
    assert _rows(NAME)[-1]["status"] == "unknown"

    _answered_once()                                              # the setup works
    await live_sched._cycle()
    assert _rows(NAME)[-1]["status"] == "operational"

    _remember("down", ms=None, error="no reaction within 20 s")   # and then it stops
    await live_sched._cycle()
    assert (_rows(NAME)[-1]["status"], _rows(NAME)[-1]["error"]) == ("down", "no reaction within 20 s")


@pytest.mark.asyncio
async def test_a_new_webhook_has_to_be_answered_again(monkeypatch, live_sched, respx_mock):
    """A new webhook is a new channel, maybe a new server."""
    _healthy(respx_mock)
    _answered_once()
    other = WEBHOOK.replace("123456789012345678", "987654321098765432")
    monkeypatch.setenv("LIVE_TEST_WEBHOOK_URL", other)
    reset_settings()
    moved = Scheduler(get_settings())
    _remember("down", ms=None, error="no reaction within 20 s")
    await moved._cycle()
    assert (_rows(NAME)[-1]["status"], _rows(NAME)[-1]["error"]) == ("unknown", "never answered")

    _answered_once(other)
    _remember("down", ms=None, error="no reaction within 20 s")
    await moved._cycle()
    assert _rows(NAME)[-1]["status"] == "down"


@pytest.mark.parametrize(("status", "arms"), [("operational", True), ("degraded", True), ("down", False), ("unknown", False)])
def test_only_an_answer_proves_the_setup(status, arms):
    live_test.remember(ProbeResult(service_name=NAME, status=status, response_ms=900, source="live_test"), WEBHOOK)
    assert live_test.answered_before(WEBHOOK) is arms


def test_the_first_answer_is_kept_and_only_the_webhooks_id_is_stored():
    _answered_once()
    first = db.kv_get(live_test.ANSWERED_KEY)
    live_test.remember(ProbeResult(service_name=NAME, status="operational", response_ms=400, source="live_test"), WEBHOOK)
    assert db.kv_get(live_test.ANSWERED_KEY) == first
    assert json.loads(first)["webhook_id"] == "123456789012345678"
    assert TOKEN not in first


@pytest.mark.parametrize("stored", ["", "not json", "[]", '{"webhook_id": "1"}', '{"at": "x"}'])
def test_a_damaged_or_foreign_record_is_not_an_answer(stored):
    if stored:
        db.kv_set(live_test.ANSWERED_KEY, stored)
    assert live_test.answered_before(WEBHOOK) is False


@pytest.mark.parametrize(("base", "expected"), [
    (WEBHOOK, "123456789012345678"),
    (WEBHOOK.replace("/api/", "/api/v10/"), "123456789012345678"),
    (f"http://127.0.0.1:8770/api/webhooks/555555555555555555/{TOKEN}", "555555555555555555"),
    ("https://discord.com/channels/1/2", ""),
    ("", ""),
    (None, ""),
])
def test_the_webhooks_id_is_read_from_its_address(base, expected):
    assert live_test.webhook_id(base) == expected


@pytest.mark.asyncio
async def test_if_the_record_cannot_be_read_the_result_is_believed(monkeypatch, live_sched, respx_mock):
    """Failing to read whether it ever worked must not hide a real outage."""
    _healthy(respx_mock)
    _remember("down", ms=None, error="no reaction within 20 s")

    def broken(base):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(live_test, "answered_before", broken)
    await live_sched._cycle()
    assert _rows(NAME)[-1]["status"] == "down"


@pytest.mark.asyncio
async def test_no_recent_run_is_no_data_not_the_last_result(live_sched, respx_mock):
    _healthy(respx_mock)
    await live_sched._cycle()                                   # the test has not finished a run yet
    assert (_rows(NAME)[-1]["status"], _rows(NAME)[-1]["error"]) == ("unknown", "no recent test")

    _remember("operational")
    held = json.loads(db.kv_get(live_test.LATEST_KEY))
    held["at"] = (datetime.now(timezone.utc) - timedelta(seconds=live_sched._live_test_fresh_seconds() + 5)).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")
    db.kv_set(live_test.LATEST_KEY, json.dumps(held))
    await live_sched._cycle()                                   # the loop stopped a while ago
    assert _rows(NAME)[-1]["status"] == "unknown"


@pytest.mark.asyncio
async def test_a_test_that_could_not_be_run_shows_as_no_data_with_a_reason(live_sched, respx_mock):
    _healthy(respx_mock)
    _remember("unknown", ms=None, error="could not post the test message (HTTP 500)")
    await live_sched._cycle()
    snap = build_snapshot()
    check = next(k for c in snap["components"] for k in c["checks"] if k["name"] == NAME)
    assert check["status"] == "unknown"
    assert check["note"] == "The test could not be run, so there is no result. That is not a sign the bot is down."
    assert snap["overall"] == "operational"                     # the other checks of that component still vouch for it
    assert incident_events(days=1) == []


@pytest.mark.asyncio
async def test_with_our_own_connection_down_the_last_result_is_not_passed_off_as_current(live_sched, respx_mock):
    respx_mock.get(f"{BASE}/readiness").mock(side_effect=httpx.ConnectError("no route"))
    for url in CONTROLS:
        respx_mock.get(url).mock(side_effect=httpx.ConnectError("no route"))
    _remember("operational")
    await live_sched._cycle()
    row = _rows(NAME)[-1]
    assert (row["status"], row["error"]) == ("unknown", "monitor offline")


@pytest.mark.asyncio
async def test_the_loop_runs_the_test_and_remembers_the_result(live_sched, respx_mock, monkeypatch):
    discord = FakeDiscord(respx_mock, react_on_read=1)
    assert live_test.latest(600) is None
    task = asyncio.create_task(live_sched.run_live_test_forever())
    for _ in range(200):
        await asyncio.sleep(0.01)
        if live_test.latest(600) is not None:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert live_test.latest(600).status == "operational"
    assert len(discord.posted) == 1 and discord.deleted == ["1001"]
    assert live_test.answered_before(WEBHOOK), "the first answered test is written down by the loop itself"


@pytest.mark.asyncio
async def test_a_run_that_raises_does_not_end_the_loop(live_sched, monkeypatch):
    calls = {"n": 0}

    async def flaky(client, base, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("unexpected")
        return ProbeResult(service_name=NAME, status="operational", response_ms=500, source="live_test")

    real_sleep = asyncio.sleep
    monkeypatch.setattr(live_test, "probe_live_test", flaky)
    monkeypatch.setattr("status_service.scheduler.asyncio.sleep", lambda seconds: real_sleep(0))
    task = asyncio.create_task(live_sched.run_live_test_forever())
    for _ in range(200):
        await real_sleep(0.005)
        if calls["n"] >= 2:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls["n"] >= 2 and live_test.latest(600).status == "operational"


def test_the_service_starts_and_stops_with_the_test_switched_off():
    with TestClient(app) as client:
        assert client.get("/health").json()["ok"] is True


def test_the_address_is_not_in_anything_the_public_can_read(monkeypatch):
    monkeypatch.setenv("LIVE_TEST_WEBHOOK_URL", WEBHOOK)
    reset_settings()
    _remember("unknown", ms=None, error="could not post the test message (HTTP 404)")
    with db.connect() as conn:
        conn.execute("INSERT INTO probe_results(service_name,status,error,source) VALUES (?,?,?,?)",
                     (NAME, "unknown", "could not post the test message (HTTP 404)", "live_test"))
    with TestClient(app) as client:
        for path in ("/", "/live", "/api", "/api/incidents", "/history", "/feed.xml", "/health"):
            text = client.get(path).text
            assert TOKEN not in text and "123456789012345678" not in text, path
