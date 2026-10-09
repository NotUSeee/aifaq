"""The live test: does a message in Discord still get an answer from the bot?

Every other check looks at one part: a process is running, a shard is
connected, a queue has a reader. All of them can pass while nothing a server
does gets answered. This is the one check that uses the product the way a
server does.

It posts a message in a private Discord server through a webhook and waits
for the bot to react to it. For that reaction to appear, Discord has to
deliver the message to the gateway, the gateway has to queue it, a worker has
to pick it up and queue the reaction, and the bot has to send it to Discord.

It needs nothing from the platform except that reaction: no token and no
access. The webhook's address is the only secret, and all it can do is post
in that one channel.

What a result means:

* reaction seen                      -> operational (degraded when it was slow)
* no reaction, twice in a row        -> down
* the test could not be run at all   -> no data: Discord refused the message
  or we could not read it back. That says nothing about the bot.

One more rule sits in the scheduler: "down" only counts once the bot has
answered through THIS webhook at least once (see answered_before). A test
that has never worked is a setup that is not finished (a missing permission,
a wrong id on the platform side), and that must not be published as an outage.

The time recorded is when the reaction was first SEEN, so it is an upper
bound: the message is read back every half second at first, then less often.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx

from .. import db
from . import USER_AGENT, ProbeResult

logger = logging.getLogger("status_service.live_test")

SERVICE_NAME = "Bot Response"
SOURCE = "live_test"
LATEST_KEY = "live_test.latest"
ANSWERED_KEY = "live_test.answered"     # the id of the webhook the bot has answered through, and when

# Seconds after the message was posted at which it is read back.
POLL_AT = (0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.5, 8.0, 10.0, 12.5, 15.0, 17.5, 20.0, 25.0, 30.0, 40.0, 50.0, 60.0)
SUPPRESS_NOTIFICATIONS = 1 << 12       # nobody in the test server gets pinged once a minute
RETRY_PAUSE_SECONDS = 1.0              # between an unanswered message and the one that confirms it
# Reads that hang (each has its own timeout) must not stretch one run to minutes:
# past the deadline plus this much, no further read is started.
OVERRUN_SLACK_SECONDS = 2.0

_DISCORD_HOSTS = ("discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com")
_WEBHOOK_PATH = re.compile(r"^/api(?:/v\d{1,2})?/webhooks/(\d{5,25})/([A-Za-z0-9_-]{20,})/?$")


def parse_webhook_url(url: str | None) -> str | None:
    """The webhook's address without query or trailing slash, or None when it
    is not a Discord webhook. Checked so a typo cannot make this service post
    a message a minute to some other address.

    One exception: plain http to THIS machine (127.0.0.1 or localhost, with a
    port). That is a stand-in for Discord in a local rehearsal. It cannot send
    anything anywhere else, which is what the check is for."""
    parts = urlsplit((url or "").strip())
    host = (parts.hostname or "").lower()
    if not _WEBHOOK_PATH.match(parts.path):
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    if parts.scheme == "https" and host in _DISCORD_HOSTS and port in (None, 443):
        return f"https://{host}{parts.path.rstrip('/')}"
    if parts.scheme == "http" and host in ("127.0.0.1", "localhost") and port and not parts.username:
        return f"http://{host}:{port}{parts.path.rstrip('/')}"
    return None


@dataclass
class Attempt:
    answered: bool | None       # None: the test could not be run
    seconds: float | None = None
    detail: str | None = None


def _reacted(message: dict, emoji: str) -> bool:
    for reaction in message.get("reactions") or []:
        if not isinstance(reaction, dict):
            continue
        name = (reaction.get("emoji") or {}).get("name") if isinstance(reaction.get("emoji"), dict) else None
        try:
            count = int(reaction.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        if name == emoji and count >= 1:
            return True
    return False


async def _delete(client: httpx.AsyncClient, base: str, message_id: str) -> None:
    """Take the test message away again. Best effort: a leftover message in a
    private channel harms nobody, and it must not change the verdict."""
    try:
        await client.delete(f"{base}/messages/{message_id}", headers={"User-Agent": USER_AGENT}, timeout=8.0)
    except httpx.HTTPError:
        # No traceback: an httpx error can carry the address, and the address is the secret.
        logger.debug("could not delete the test message")


async def attempt_once(client: httpx.AsyncClient, base: str, *, emoji: str, deadline: float) -> Attempt:
    """Post one test message and wait up to `deadline` seconds for the reaction."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    try:
        posted = await client.post(
            base, params={"wait": "true"}, timeout=10.0,
            headers={"User-Agent": USER_AGENT},
            json={"content": f"Status check {stamp}", "allowed_mentions": {"parse": []},
                  "flags": SUPPRESS_NOTIFICATIONS},
        )
    except httpx.HTTPError as exc:
        return Attempt(None, detail=f"could not post the test message ({type(exc).__name__})")
    if posted.status_code != 200:
        if posted.status_code in (401, 403, 404):
            logger.error("the live test's webhook was refused (HTTP %d): it was deleted or its address is wrong",
                         posted.status_code)
        return Attempt(None, detail=f"could not post the test message (HTTP {posted.status_code})")
    try:
        message_id = str(posted.json()["id"])
    except (ValueError, KeyError, TypeError):
        return Attempt(None, detail="could not post the test message (no message id came back)")
    if not message_id.isdigit():
        return Attempt(None, detail="could not post the test message (no message id came back)")

    started = time.monotonic()
    read_ok = False
    try:
        for offset in POLL_AT:
            if offset > deadline or time.monotonic() - started > deadline + OVERRUN_SLACK_SECONDS:
                break
            wait = offset - (time.monotonic() - started)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                got = await client.get(f"{base}/messages/{message_id}", headers={"User-Agent": USER_AGENT}, timeout=8.0)
            except httpx.HTTPError:
                continue
            if got.status_code == 404:
                # Someone removed it. Whether the bot would have reacted cannot be known.
                return Attempt(None, detail="the test message disappeared before it could be read")
            if got.status_code == 429:
                try:
                    pause = min(5.0, max(0.0, float(got.json().get("retry_after") or 1.0)))
                except (ValueError, TypeError, AttributeError):
                    pause = 1.0
                await asyncio.sleep(pause)
                continue
            if got.status_code != 200:
                continue
            try:
                message = got.json()
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            read_ok = True
            if _reacted(message, emoji):
                return Attempt(True, seconds=time.monotonic() - started)
    finally:
        await _delete(client, base, message_id)
    if not read_ok:
        return Attempt(None, detail="could not read the test message back")
    return Attempt(False, detail=f"no reaction within {int(deadline)} s")


async def probe_live_test(
    client: httpx.AsyncClient, base: str, *,
    emoji: str = "\u2705", deadline: float = 20.0, slow_after: float = 5.0, attempts: int = 2,
) -> ProbeResult:
    """Run the test. A missing reaction is believed only when a second
    message went unanswered too: one lost event is not an outage."""
    outcomes: list[Attempt] = []
    for number in range(max(1, attempts)):
        if number:
            await asyncio.sleep(RETRY_PAUSE_SECONDS)
        outcome = await attempt_once(client, base, emoji=emoji, deadline=deadline)
        outcomes.append(outcome)
        if outcome.answered:
            status = "operational" if (outcome.seconds or 0.0) <= slow_after else "degraded"
            return ProbeResult(service_name=SERVICE_NAME, status=status, source=SOURCE,
                               response_ms=int(round((outcome.seconds or 0.0) * 1000)),
                               extra={"attempts": number + 1})
    if all(o.answered is False for o in outcomes):
        return ProbeResult(service_name=SERVICE_NAME, status="down", source=SOURCE,
                           error=outcomes[-1].detail, extra={"attempts": len(outcomes)})
    detail = next((o.detail for o in outcomes if o.answered is None and o.detail), "the test could not be run")
    return ProbeResult(service_name=SERVICE_NAME, status="unknown", source=SOURCE, error=detail,
                       extra={"attempts": len(outcomes)})


# ── The latest result, for the scheduler's next cycle ─────────────────────

def webhook_id(base: str | None) -> str:
    """The webhook's id out of its address. Not a secret (the token after it is)."""
    match = _WEBHOOK_PATH.match(urlsplit(base or "").path)
    return match.group(1) if match else ""


def remember(result: ProbeResult, base: str | None = None) -> None:
    """Keep the result for the scheduler's next cycle. The first answered test
    through a webhook is also written down for good: from then on a missing
    answer through that webhook is believed."""
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    db.kv_set(LATEST_KEY, json.dumps({
        "at": now, "status": result.status, "response_ms": result.response_ms, "error": result.error,
    }, separators=(",", ":")))
    hook = webhook_id(base)
    if hook and result.status in ("operational", "degraded") and not answered_before(base):
        db.kv_set(ANSWERED_KEY, json.dumps({"webhook_id": hook, "at": now}, separators=(",", ":")))


def answered_before(base: str | None) -> bool:
    """Has the bot ever answered a test posted through this webhook?

    Tied to the webhook, not just "ever": a new webhook means a new channel,
    maybe a new server, and the setup has to prove itself again before its
    silence is called an outage."""
    hook = webhook_id(base)
    raw = db.kv_get(ANSWERED_KEY)
    if not hook or not raw:
        return False
    try:
        held = json.loads(raw)
    except ValueError:
        return False
    return isinstance(held, dict) and held.get("webhook_id") == hook


def latest(max_age_seconds: float) -> ProbeResult | None:
    """The last finished test, if it is recent enough to describe now."""
    raw = db.kv_get(LATEST_KEY)
    if not raw:
        return None
    try:
        held = json.loads(raw)
        at = str(held["at"])
        age = (datetime.now(timezone.utc)
               - datetime.fromisoformat(at[:-1] + "+00:00" if at.endswith("Z") else at)).total_seconds()
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(held, dict) or age > max_age_seconds or age < -60:
        return None
    status = held.get("status")
    if status not in ("operational", "degraded", "down", "unknown"):
        return None
    ms = held.get("response_ms")
    return ProbeResult(service_name=SERVICE_NAME, status=status, source=SOURCE,
                       response_ms=int(ms) if isinstance(ms, (int, float)) and not isinstance(ms, bool) else None,
                       error=held.get("error") if isinstance(held.get("error"), str) else None)
