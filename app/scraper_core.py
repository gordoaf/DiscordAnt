"""Scrape engine for the GUI app.

How it works: a real Chromium window scrolls a channel with PageUp at human
pace while we passively read the /messages JSON that Discord's own client
fetches. Nothing here ever calls Discord's API itself.

On top of that:

  - Everything captured goes into a local archive (store.py), which also
    remembers which stretches of each channel are fully covered. A job works
    out what is still MISSING from its date range and only scrolls through
    that — so a stopped job resumes where it left off, and a range that was
    scraped before is not scraped again.
  - Pause / resume (the browser stays open while paused).
  - Channel and server names are read off the page.
  - Pictures the browser loads while scrolling are kept from the browser's own
    copy (no extra request). The optional full-size pass afterwards uses a
    separate, bounded HTTPS downloader that carries no Discord login and
    never renders web content.
  - Mentions, reactions, stickers, forwarded messages and polls are kept.

The output file for a job is always the complete requested range, written
from the archive.
"""

import asyncio
import http.client
import json
import math
import random
import re
import ssl
import threading
import time
import weakref
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse, urlsplit

from playwright.async_api import async_playwright

try:
    from . import exports, store as store_mod
except ImportError:  # running as a plain script / frozen exe
    import exports
    import store as store_mod

MSG_PATH_RE = re.compile(r"/api/v\d+/channels/(\d+)/messages")
SEARCH_PATH_RE = re.compile(r"/api/v\d+/guilds/(\d+)/messages/search")
LINK_PATH_RE = re.compile(r"/channels/(\d+)/(\d+)(?:/\d+)?/?")
ATTACH_PATH_RE = re.compile(r"^/(?:ephemeral-)?attachments/(\d+)/(\d+)/")
DISCORD_EPOCH_MS = 1420070400000
IMAGE_HOSTS = frozenset({"cdn.discordapp.com", "cdn.discordapp.net",
                         "media.discordapp.net", "images-ext-1.discordapp.net",
                         "images-ext-2.discordapp.net"})
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_SESSION_IMAGE_BYTES = 100 * 1024 * 1024
IMAGE_DOWNLOAD_TIMEOUT_S = 20
IMAGE_CHUNK_BYTES = 64 * 1024
IMAGE_READS_AT_ONCE = 4                 # browser-loaded pictures copied at one time
IMAGE_INDEX_WAIT_S = 30                 # how long a picture waits for its message list
CLEANUP_TIMEOUT_S = 5

# Pacing — deliberately human-slow. Do not "optimize" these down.
SCROLL_MIN_S, SCROLL_MAX_S = 2.5, 5.5
BREAK_EVERY_MIN, BREAK_EVERY_MAX = 20, 35
BREAK_MIN_S, BREAK_MAX_S = 20, 60
IDLE_STEPS_LIMIT = 15
IMAGE_MIN_S, IMAGE_MAX_S = 1.5, 4.0     # between full-size image loads

# Discord sometimes shows a red "messages failed to load / Retry" banner when a
# history fetch fails or gets rate limited. Clicking Retry and waiting beats
# scrolling on obliviously, so those steps don't count toward IDLE_STEPS_LIMIT.
ERROR_BACKOFF_MIN_S, ERROR_BACKOFF_MAX_S = 12, 25
MAX_ERROR_RETRIES = 25

# How many times to jump back to the same spot before giving up on it.
MAX_JUMP_TRIES = 3
# Already-collected history longer than this many messages is jumped over
# instead of scrolled through.
SKIP_BY_JUMP_MIN = 150
# "Now" is taken a little in the past so a slightly fast PC clock can never
# mark messages that do not exist yet as already collected.
NOW_MARGIN_S = 60

# How a scrape ended. Only END_DATE/TOP mean "we got everything asked for".
END_DATE, TOP, STOPPED, BUDGET, STALLED = ("end_date", "top", "stopped",
                                           "budget", "stalled")

# A retry control inside Discord's error banner. Restricted to buttons outside
# the message list so a user typing "retry" in chat can't trigger a false match.
JS_FIND_RETRY = """() => {
    const inMessage = (el) => !!el.closest('li[class*="messageListItem"]');
    for (const el of document.querySelectorAll('button, [role="button"], a')) {
        const t = (el.innerText || "").trim();
        if (t && /^(retry|try again|reload)$/i.test(t) && !inMessage(el)) {
            el.click();
            return true;
        }
    }
    return false;
}"""

JS_SCROLLER = """() => {
    const list = document.querySelector('[data-list-id="chat-messages"]');
    if (!list) return null;
    let el = list.parentElement;
    while (el && el.scrollHeight <= el.clientHeight + 100) el = el.parentElement;
    return {st: el ? Math.round(el.scrollTop) : -1};
}"""

# Channel names as the sidebar shows them: [[channel_id, name], ...]
JS_SIDEBAR = """(g) => [...document.querySelectorAll('a[href^="/channels/' + g + '/"]')]
    .map(a => [a.getAttribute('href').split('/').pop(),
               (a.innerText || '').split('\\n').pop().trim()])"""

JS_LIST_LABEL = """() => {
    const l = document.querySelector('ol[data-list-id="chat-messages"]');
    return l ? l.getAttribute('aria-label') : null;
}"""

SYSTEM_TYPES = {6: "pinned a message", 7: "joined the server",
                8: "boosted the server", 9: "boosted the server",
                10: "boosted the server", 11: "boosted the server",
                12: "followed a channel", 18: "started a thread"}

IMAGE_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
             "image/webp": ".webp", "image/gif": ".gif", "image/avif": ".avif",
             "image/bmp": ".bmp"}


# ------------------------------------------------------------------ helpers
def _https_url(url, hosts):
    """Parse only an exact HTTPS origin, without credentials or odd ports."""
    if not isinstance(url, str) or not url or any(c.isspace() or ord(c) < 32
                                               for c in url) or "\\" in url:
        return None
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname not in hosts
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443) or parsed.fragment):
            return None
        return parsed
    except ValueError:
        return None


def _discord_url(url):
    return _https_url(url, {"discord.com"})


def _image_url(url):
    parsed = _https_url(url, IMAGE_HOSTS)
    if parsed and (ATTACH_PATH_RE.match(parsed.path)
                   or parsed.path.startswith("/external/")):
        return parsed
    return None


def _valid_id(value):
    text = str(value) if value is not None else ""
    return (len(text) <= 19 and text.isascii() and text.isdecimal()
            and 0 < int(text) <= 2**63 - 1)


def parse_channel_link(link):
    """Return (guild_id, channel_id) from a discord.com/channels/... URL."""
    link = link.strip()
    if link.startswith("discord.com/"):
        link = "https://" + link
    parsed = _discord_url(link)
    m = LINK_PATH_RE.fullmatch(parsed.path) if parsed else None
    if not m or not all(_valid_id(v) for v in m.groups()):
        raise ValueError("That doesn't look like a Discord channel link "
                         "(expected .../channels/<server>/<channel>).")
    return m.group(1), m.group(2)


def dt_to_snowflake(dt):
    ms = int(dt.astimezone(timezone.utc).timestamp() * 1000)
    return max(0, (ms - DISCORD_EPOCH_MS) << 22)


def snowflake_to_dt(sf):
    return datetime.fromtimestamp(((int(sf) >> 22) + DISCORD_EPOCH_MS) / 1000,
                                  tz=timezone.utc)


def now_snowflake():
    return dt_to_snowflake(datetime.now(timezone.utc) - timedelta(seconds=NOW_MARGIN_S))


def parse_ts(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def window_ids(start_dt, end_dt):
    """(lo, hi) message-id range for a job: end_dt is the OLDER bound, start_dt
    the NEWER one; either may be None (channel beginning / right now)."""
    lo = dt_to_snowflake(end_dt) if end_dt else 0
    hi = now_snowflake()
    if start_dt:
        hi = min(hi, dt_to_snowflake(start_dt + timedelta(seconds=1)) - 1)
    return lo, hi


def pretty_channel(name):
    """'🌎│climate' -> 'climate' (drop the emoji/divider decoration)."""
    name = (name or "").strip()
    for sep in ("│", "┃", "｜", "︱", "|", "・", "┊", "╏"):
        if sep in name:
            tail = name.split(sep)[-1].strip()
            if tail:
                name = tail
    stripped = re.sub(r"^[^\w]+", "", name, flags=re.UNICODE)
    return stripped or name


def safe_name(text):
    out = "".join(c if (c.isalnum() or c in "-_") else "_" for c in (text or ""))
    return re.sub(r"_+", "_", out).strip("_") or "x"


# ------------------------------------------------------------------ records
MENTION_RE = re.compile(r"<@!?(\d+)>")
CHANNEL_REF_RE = re.compile(r"<#(\d+)>")
EMOJI_RE = re.compile(r"<a?:(\w+):\d+>")
ROLE_RE = re.compile(r"<@&(\d+)>")
TIME_RE = re.compile(r"<t:(\d+)(?::[a-zA-Z])?>")


def _display(user):
    return user.get("global_name") or user.get("username") or "?"


def _resolve_mentions(content, mentions):
    if not content or "<@" not in content:
        return content or ""
    names = {str(m.get("id")): _display(m) for m in mentions or [] if m.get("id")}
    return MENTION_RE.sub(
        lambda m: "@" + names[m.group(1)] if names.get(m.group(1)) else m.group(0),
        content)


def _norm_attachment(a):
    out = {"id": a.get("id"), "name": a.get("filename"),
           "ctype": a.get("content_type"), "url": a.get("url"),
           "proxy": a.get("proxy_url"), "w": a.get("width"), "h": a.get("height"),
           "size": a.get("size"), "alt": a.get("description")}
    return {k: v for k, v in out.items() if v not in (None, "")}


def _norm_embed(e):
    out = {}
    kind = e.get("type")
    if kind:
        out["type"] = kind
    for src, dst in (("title", "title"), ("description", "desc"), ("url", "url")):
        if e.get(src):
            out[dst] = e[src]
    for src, key, dst in (("author", "name", "author"), ("provider", "name", "provider"),
                          ("footer", "text", "footer")):
        val = (e.get(src) or {}).get(key)
        if val:
            out[dst] = val
    fields = [[f.get("name") or "", f.get("value") or ""] for f in e.get("fields") or []]
    if fields:
        out["fields"] = fields
    # Pictures worth keeping: a bare image link, a GIF, or an embed's own image
    # (tweets, bot charts). Site-preview thumbnails are skipped.
    img = e.get("image") or (e.get("thumbnail") if kind in ("image", "gifv") else None)
    if img and (img.get("proxy_url") or img.get("url")):
        out["img"] = {k: v for k, v in (("url", img.get("url")),
                                        ("proxy", img.get("proxy_url")),
                                        ("w", img.get("width")),
                                        ("h", img.get("height"))) if v}
    if e.get("video") and kind in ("gifv", "video"):
        out["video"] = True
    return out


def _poll_text(poll):
    try:
        q = (poll.get("question") or {}).get("text") or ""
        counts = {c.get("id"): c.get("count") for c in
                  ((poll.get("results") or {}).get("answer_counts") or [])}
        answers = []
        for a in poll.get("answers") or []:
            text = (a.get("poll_media") or {}).get("text") or ""
            n = counts.get(a.get("answer_id"))
            answers.append(f"{text} ({n})" if n is not None else text)
        return f"{q} | " + " / ".join(answers) if answers else q
    except Exception:
        return ""


def normalize(msg):
    """Discord's message JSON -> the compact record kept in the archive."""
    author = msg.get("author") or {}
    rec = {
        "id": str(msg["id"]),
        "ts": msg.get("timestamp"),
        "author": _display(author),
        "author_id": author.get("id"),
        "username": author.get("username"),
        "content": _resolve_mentions(msg.get("content"), msg.get("mentions")),
    }
    if author.get("bot"):
        rec["bot"] = True
    kind = msg.get("type") or 0
    if kind not in (0, 19, 20):
        rec["type"] = kind
    if msg.get("edited_timestamp"):
        rec["edited"] = True

    mref = msg.get("message_reference") or {}
    if mref.get("message_id"):
        rec["reply_id"] = str(mref["message_id"])
        if mref.get("channel_id") and str(mref["channel_id"]) != str(msg.get("channel_id")):
            rec["reply_channel"] = str(mref["channel_id"])
    ref = msg.get("referenced_message")
    if ref:
        text = _resolve_mentions(ref.get("content"), ref.get("mentions"))
        if not text and ref.get("attachments"):
            text = "[" + ", ".join(a.get("filename") or "file"
                                   for a in ref["attachments"]) + "]"
        rec["reply_to"] = {"author": _display(ref.get("author") or {}),
                           "content": text[:300]}

    atts = [_norm_attachment(a) for a in msg.get("attachments") or []]
    embeds = [_norm_embed(e) for e in msg.get("embeds") or []]

    # A forwarded message carries its real content in a snapshot.
    for snap in msg.get("message_snapshots") or []:
        inner = snap.get("message") or {}
        text = _resolve_mentions(inner.get("content"), inner.get("mentions"))
        rec["forwarded"] = text or True
        atts += [dict(_norm_attachment(a), fwd=True) for a in inner.get("attachments") or []]
        embeds += [_norm_embed(e) for e in inner.get("embeds") or []]

    atts = [a for a in atts if a.get("name") or a.get("id")]
    embeds = [e for e in embeds if e]
    if atts:
        rec["attachments"] = atts
    if embeds:
        rec["embeds"] = embeds

    reactions = []
    for r in msg.get("reactions") or []:
        name = (r.get("emoji") or {}).get("name")
        if name:
            reactions.append([name, r.get("count") or 1])
    if reactions:
        rec["reactions"] = reactions
    stickers = [s.get("name") for s in msg.get("sticker_items") or [] if s.get("name")]
    if stickers:
        rec["stickers"] = stickers
    if msg.get("poll"):
        text = _poll_text(msg["poll"])
        if text:
            rec["poll"] = text
    if (msg.get("thread") or {}).get("name"):
        rec["thread"] = msg["thread"]["name"]
    return rec


def message_users(msg):
    """(id, username, global_name) for everyone named in a raw message."""
    out = []
    people = [msg.get("author") or {}] + list(msg.get("mentions") or [])
    ref = msg.get("referenced_message") or {}
    if ref.get("author"):
        people.append(ref["author"])
    for u in people:
        if u.get("id"):
            out.append((u["id"], u.get("username"), u.get("global_name")))
    return out


def clean_text(text, store=None):
    """Turn Discord's raw markup into something readable."""
    if not text or "<" not in text:
        return text or ""

    def chan(m):
        name = store.name(m.group(1)) if store else None
        return "#" + (pretty_channel(name) if name else m.group(1))

    def user(m):
        row = store.user(m.group(1)) if store else None
        return "@" + ((row[2] or row[1]) if row and (row[2] or row[1]) else m.group(1))

    def when(m):
        try:
            return datetime.fromtimestamp(int(m.group(1)), tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M UTC")
        except (ValueError, OverflowError, OSError):
            return m.group(0)

    text = CHANNEL_REF_RE.sub(chan, text)
    text = EMOJI_RE.sub(lambda m: f":{m.group(1)}:", text)
    text = ROLE_RE.sub("@role", text)
    text = TIME_RE.sub(when, text)
    return MENTION_RE.sub(user, text)


def embed_text(e):
    if isinstance(e, str):            # a record stored as plain text
        return e
    parts = [e.get("author"), e.get("title"), e.get("desc")]
    parts += [f"{n}: {v}" if n else v for n, v in e.get("fields") or []]
    parts.append(e.get("footer"))
    return " | ".join(p for p in parts if p)[:600]


def reply_text(rec, channel_id, store, limit=120):
    """'author: text' for the message this one replies to, or None."""
    ref = rec.get("reply_to")
    if isinstance(ref, str):          # a record stored as plain text
        return ref
    if isinstance(ref, dict):
        return f"{ref.get('author') or '?'}: {clean_text(ref.get('content') or '', store)[:limit]}"
    if rec.get("reply_id") and store is not None:
        target = store.get(rec.get("reply_channel") or channel_id, rec["reply_id"])
        if target:
            text = clean_text(target.get("content") or "", store)
            if not text and target.get("attachments"):
                text = "[" + ", ".join(attachment_names(target)) + "]"
            return f"{target.get('author') or '?'}: {text[:limit]}"
    return None


def attachment_names(rec):
    names = [a.get("name") if isinstance(a, dict) else a
             for a in rec.get("attachments") or []]
    return [n for n in names if n]


def image_entries(rows):
    """Archive image rows -> the small dicts written into output files."""
    out = []
    for r in rows:
        if r.get("file"):
            out.append({"name": r.get("name") or "", "file": r["file"]})
    return out


def export_row(rec, channel_id, store, images=None):
    """Archive record -> one row of an output file (same keys as always, plus
    a few optional extras: images, reactions, stickers, forwarded, poll)."""
    row = {
        "id": rec["id"],
        "ts": (rec.get("ts") or "")[:16],
        "author": rec.get("author") or "?",
        "content": clean_text(rec.get("content") or "", store),
    }
    if not row["content"] and rec.get("type") in SYSTEM_TYPES:
        row["content"] = f"({SYSTEM_TYPES[rec['type']]})"
    if rec.get("bot"):
        row["bot"] = True
    reply = reply_text(rec, channel_id, store)
    if reply:
        row["reply_to"] = reply
    names = attachment_names(rec)
    if names:
        row["attachments"] = names
    embeds = [t for t in (embed_text(e) for e in rec.get("embeds") or []) if t]
    if embeds:
        row["embeds"] = embeds
    if rec.get("forwarded"):
        row["forwarded"] = (clean_text(rec["forwarded"], store)
                            if isinstance(rec["forwarded"], str) else "(forwarded message)")
    if rec.get("stickers"):
        row["stickers"] = rec["stickers"]
    if rec.get("poll"):
        row["poll"] = rec["poll"]
    if rec.get("thread"):
        row["thread"] = rec["thread"]
    if rec.get("reactions"):
        row["reactions"] = " ".join(f"{n}{c}" for n, c in rec["reactions"])
    if images is None and store is not None:
        images = store.images_for(channel_id, rec["id"])
    entries = image_entries(images or [])
    if entries:
        row["images"] = entries
    return row


def export_channel(store, channel_id, lo, hi, out_path, log):
    """Write every archived message of a channel inside [lo, hi] to out_path."""
    by_msg = {}
    for im in store.images_in(channel_id, lo, hi):
        by_msg.setdefault(str(im["message_id"]), []).append(im)
    rows = [export_row(rec, channel_id, store, by_msg.get(rec["id"], []))
            for rec in store.messages_in(channel_id, lo, hi)]
    out_path = Path(out_path)
    exports.write_text(out_path, json.dumps(rows, ensure_ascii=False, indent=1))
    if rows:
        log(f"Saved {len(rows)} messages ({rows[0]['ts']} -> {rows[-1]['ts']})")
    else:
        log("No messages in that range.")
    log(f"Output: {out_path}")
    return len(rows)


# ------------------------------------------------------------------ pacing
class PacerInterrupted(Exception):
    """A browser/download operation ended because its job stopped or expired."""

    def __init__(self, outcome):
        super().__init__(outcome)
        self.outcome = outcome


class Pacer:
    """Human-paced waits that honour Stop, Pause and the per-job time limit."""

    def __init__(self, stop_event, pause_event=None, log=print, max_minutes=None):
        self.stop_event = stop_event
        self.pause_event = pause_event
        self.log = log
        if max_minutes is not None and (not math.isfinite(max_minutes) or max_minutes < 0):
            raise ValueError("Time limit must be a finite, nonnegative number.")
        self.deadline = (time.monotonic() + max_minutes * 60
                         if max_minutes is not None else None)
        self.steps_until_break = random.randint(BREAK_EVERY_MIN, BREAK_EVERY_MAX)

    @property
    def stopped(self):
        return self.stop_event.is_set()

    @property
    def paused(self):
        return bool(self.pause_event is not None and self.pause_event.is_set())

    def out_of_time(self):
        return self.deadline is not None and time.monotonic() >= self.deadline

    def remaining(self):
        return (max(0.0, self.deadline - time.monotonic())
                if self.deadline is not None else None)

    def outcome(self):
        return STOPPED if self.stopped else BUDGET if self.out_of_time() else None

    async def checkpoint(self):
        """Cooperate with pause, Stop and the active-time budget."""
        if self.outcome() is not None:
            return False
        await self.hold_if_paused()
        return self.outcome() is None

    async def run(self, awaitable, timeout=None):
        """Bound an operation by its timeout, Stop and this job's budget."""
        if not await self.checkpoint():
            if hasattr(awaitable, "close"):
                awaitable.close()
            elif hasattr(awaitable, "cancel"):
                awaitable.cancel()
            raise PacerInterrupted(self.outcome())
        task = asyncio.ensure_future(awaitable)
        operation_end = time.monotonic() + timeout if timeout is not None else None
        try:
            while True:
                before_pause = time.monotonic()
                if not await self.checkpoint():
                    raise PacerInterrupted(self.outcome())
                if operation_end is not None:
                    # Explicit job pauses suspend the operation's wait budget too.
                    operation_end += max(0.0, time.monotonic() - before_pause)
                if task.done():
                    return await task
                if operation_end is not None and time.monotonic() >= operation_end:
                    raise asyncio.TimeoutError()
                wait = 0.1
                if operation_end is not None:
                    wait = min(wait, max(0.0, operation_end - time.monotonic()))
                remaining = self.remaining()
                if remaining is not None:
                    wait = min(wait, remaining)
                await asyncio.wait({task}, timeout=wait)
        finally:
            if not task.done():
                task.cancel()
                # Playwright awaits cooperate with cancellation. A broken/mock
                # operation must not keep the queue hung while it is stopping.
                await asyncio.wait({task}, timeout=0.5)
            if task.done() and not task.cancelled():
                task.exception()  # consume failures when cancellation won the race
            elif not task.done():
                task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    async def hold_if_paused(self):
        if not self.paused:
            return 0.0
        t0 = time.monotonic()
        self.log("Paused — the browser stays open. Press Resume to carry on.")
        while self.paused and not self.stopped:
            await asyncio.sleep(0.5)
        waited = time.monotonic() - t0
        if self.deadline is not None:
            self.deadline += waited      # paused time does not eat the limit
        if not self.stopped:
            self.log("Resumed.")
        return waited

    async def nap(self, lo, hi=None):
        """Sleep lo..hi seconds. False means Stop or the job budget expired."""
        secs = random.uniform(lo, hi) if hi is not None else lo
        end = time.monotonic() + secs
        while True:
            if self.outcome() is not None:
                return False
            if self.paused:
                end += await self.hold_if_paused()
                continue
            left = end - time.monotonic()
            if left <= 0:
                return True
            remaining = self.remaining()
            await asyncio.sleep(min(0.25, left, remaining if remaining is not None else left))

    async def step(self, status=None, lo=SCROLL_MIN_S, hi=SCROLL_MAX_S):
        """The wait after one action, with a longer break every so often."""
        self.steps_until_break -= 1
        if self.steps_until_break <= 0:
            self.steps_until_break = random.randint(BREAK_EVERY_MIN, BREAK_EVERY_MAX)
            pause = random.uniform(BREAK_MIN_S, BREAK_MAX_S)
            extra = status() if status else ""
            self.log(f"Short break {pause:.0f}s" + (f" — {extra}" if extra else ""))
            return await self.nap(pause)
        return await self.nap(lo, hi)


# ------------------------------------------------------------------ browser
def _url_expired(url):
    """Discord signs attachment links; `ex` is the expiry time in hex."""
    try:
        ex = parse_qs(urlparse(url).query).get("ex", [None])[0]
        return bool(ex) and int(ex, 16) < time.time() + 30
    except ValueError:
        return False


def _full_size_url(row):
    """The link to load for a full-size copy (big or animated ones scaled)."""
    url = row["url"]
    w, h = (v if isinstance(v, (int, float)) and math.isfinite(v) and 0 <= v <= 100000
            else 0 for v in (row.get("w"), row.get("h")))
    name = (row.get("name") or "").lower()
    big = max(w, h) > 1600
    parsed = _image_url(url)
    if not parsed:
        return None
    if not (big or name.endswith(".gif")) or parsed.hostname == "cdn.discordapp.com":
        return url
    extra = "format=webp"
    if w and h:
        scale = min(1.0, 1568 / max(w, h))
        extra += f"&width={max(1, round(w * scale))}&height={max(1, round(h * scale))}"
    return url + ("&" if "?" in url else "?") + extra


# A stuck OS DNS resolver must not strand the GUI/asyncio executor at exit.
# Dedicated daemon workers are capped across sessions, including abandoned ones.
_IMAGE_WORKERS = threading.BoundedSemaphore(2)


def _read_cdn_image(url, limit, deadline, cancelled):
    """Stream one inert HTTPS resource, with no profile, cookies or redirects.

    This is an independent transport, not a page: there is no JavaScript,
    navigation, subresource loading, inherited proxy or Discord login state.
    """
    parsed = _image_url(url)
    if parsed is None:
        raise ValueError("Image URL is not an allowed Discord HTTPS CDN resource.")

    def remaining():
        left = deadline - time.monotonic()
        if cancelled.is_set() or left <= 0:
            raise TimeoutError("Image download stopped or timed out.")
        return left

    connection = http.client.HTTPSConnection(parsed.hostname, 443,
                                             timeout=min(2.0, remaining()),
                                             context=ssl.create_default_context())
    try:
        target = parsed.path + ("?" + parsed.query if parsed.query else "")
        connection.request("GET", target, headers={"Accept": "image/*",
                           "Accept-Encoding": "identity", "Connection": "close"})
        remaining()
        response = connection.getresponse()
        remaining()
        # Never follow even a same-host redirect: it could change resource type
        # or lead to a different host after another hop.
        if response.status != 200:
            raise ValueError(f"Image CDN returned HTTP {response.status}.")
        ctype = response.getheader("content-type", "").split(";")[0].strip().lower()
        if ctype not in IMAGE_EXT:
            raise ValueError("Image CDN did not return a supported raster image.")
        if response.getheader("content-encoding", "identity").lower() != "identity":
            raise ValueError("Encoded image responses are not accepted.")
        length = response.getheader("content-length")
        if length is not None and (not length.isdecimal() or int(length) > limit):
            raise ValueError("Image exceeds the download size limit.")
        chunks, size = [], 0
        while True:
            left = remaining()
            sock = connection.sock
            if sock is None:
                sock = getattr(getattr(response.fp, "raw", None), "_sock", None)
            if sock is not None:
                sock.settimeout(min(2.0, left))
            chunk = response.read1(min(IMAGE_CHUNK_BYTES, limit - size + 1))
            remaining()
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                raise ValueError("Image exceeds the download size limit.")
            chunks.append(chunk)
        if length is not None and size != int(length):
            raise ValueError("Image response was truncated.")
        if not size:
            raise ValueError("Image response was empty.")
        return b"".join(chunks), ctype
    finally:
        connection.close()


class Session:
    """One browser session: launches the scraper's own Chromium profile and
    files away everything Discord's client loads while we drive the page."""

    def __init__(self, *, profile_dir, store, log, pacer, images_dir=None):
        self.profile_dir = profile_dir
        self.store = store
        self.log = log
        self.pacer = pacer
        self.images_dir = Path(images_dir) if images_dir else None
        self.fetches = []       # one dict per captured batch of channel messages
        self.searches = []      # one dict per captured search response
        self.images_saved = 0
        self._handler_errors = 0
        self._response_tasks = set()
        self._request_starts = weakref.WeakKeyDictionary()
        self._download_lock = asyncio.Lock()
        self._image_reads = asyncio.Semaphore(IMAGE_READS_AT_ONCE)
        self._filing = 0                    # message lists being read right now
        self._filed = asyncio.Event()       # set whenever one of them is done
        self._image_bytes = 0
        self._image_failures = 0
        self._image_limit_reported = False
        self._closed = False
        self._pw = None
        self.context = None
        self.page = None

    async def __aenter__(self):
        self._pw = await self.pacer.run(async_playwright().start(), timeout=30)
        try:
            self.context = await self.pacer.run(self._pw.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                headless=False,
                args=["--disable-background-timer-throttling",
                      "--disable-backgrounding-occluded-windows",
                      "--disable-renderer-backgrounding",
                      "--start-maximized"],
                no_viewport=True,
                chromium_sandbox=True,
                timeout=30000,
            ), timeout=30)
            self.page = (self.context.pages[0] if self.context.pages else
                         await self.pacer.run(self.context.new_page(), timeout=10))
        except BaseException:
            if self.context is not None:
                await self._bounded_cleanup(self.context.close())
            await self._bounded_cleanup(self._pw.stop())
            raise
        self.page.on("request", self._note_request)
        self.page.on("response", self._queue_response)
        return self

    async def _bounded_cleanup(self, awaitable):
        """Clean up even after budget expiry without hanging or masking it."""
        task = asyncio.ensure_future(awaitable)
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=CLEANUP_TIMEOUT_S)
        except Exception as exc:
            task.cancel()
            task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
            self.log(f"NOTE: browser cleanup did not finish normally ({type(exc).__name__}).")

    async def __aexit__(self, *exc):
        self._closed = True
        try:
            tasks = list(self._response_tasks)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.wait(tasks, timeout=1)
            if self.context is not None:
                await self._bounded_cleanup(self.context.close())
        finally:
            if self._pw is not None:
                await self._bounded_cleanup(self._pw.stop())
        return False

    async def open_app(self):
        """Load Discord and wait for a login if needed. False = Stop pressed."""
        try:
            await self.pacer.run(self.page.goto("https://discord.com/app"), timeout=30)
            if not await self.pacer.nap(5):
                return False
            while True:
                parsed = _discord_url(self.page.url)
                if parsed is None:
                    raise ValueError("The browser left Discord's HTTPS website.")
                if not parsed.path.startswith("/login"):
                    return await self.pacer.checkpoint()
                self.log("Waiting for Discord login (log in in the browser window)...")
                if not await self.pacer.nap(5):
                    return False
        except PacerInterrupted:
            return False

    # ---- passive capture
    def _note_request(self, request):
        self._request_starts[request] = time.monotonic()

    def _queue_response(self, resp):
        if self._closed:
            return
        task = asyncio.create_task(self._on_response(resp))
        self._response_tasks.add(task)
        task.add_done_callback(self._response_tasks.discard)

    async def _on_response(self, resp):
        url = resp.url
        try:
            request_started = self._request_starts.pop(resp.request, None)
        except (TypeError, AttributeError):
            request_started = None  # unknown/mock request: search fails closed
        try:
            parsed = _discord_url(url)
            m = MSG_PATH_RE.fullmatch(parsed.path) if parsed else None
            if m:
                if resp.status == 200 and resp.request.method == "GET":
                    # Counted as "being filed" from here until its messages
                    # are in the archive: a picture that finishes loading in
                    # between waits for this instead of finding nothing.
                    self._filing += 1
                    try:
                        data = await self.pacer.run(resp.json(), timeout=30)
                        if isinstance(data, list):
                            self._take_batch(m.group(1), url, data,
                                             request_started=request_started)
                    finally:
                        self._filing -= 1
                        self._filed.set()
                return
            m = SEARCH_PATH_RE.fullmatch(parsed.path) if parsed else None
            if m:
                if resp.request.method != "GET":
                    return
                self._filing += 1
                try:
                    try:
                        data = await self.pacer.run(resp.json(), timeout=30)
                    except PacerInterrupted:
                        return
                    except Exception:
                        data = None
                    self._take_search(m.group(1), url, resp.status, data,
                                      request_started=request_started)
                finally:
                    self._filing -= 1
                    self._filed.set()
                return
            if self.images_dir is not None and resp.status == 200:
                if _image_url(url):
                    await self._take_image(resp)
        except PacerInterrupted:
            return
        except Exception as e:   # a capture hiccup must never kill the scrape
            self._handler_errors += 1
            if self._handler_errors <= 3:
                self.log(f"NOTE: could not read one response ({type(e).__name__}: {e})")

    def _take_batch(self, channel_id, url, data, request_started=None):
        q = parse_qs(urlparse(url).query)
        try:
            limit = int(q.get("limit", ["50"])[0])
        except ValueError as exc:
            raise ValueError("Invalid Discord message-page limit.") from exc
        if (not _valid_id(channel_id) or not 1 <= limit <= 100
                or sum(k in q for k in ("before", "after", "around")) > 1
                or any(len(v) != 1 for v in q.values())
                or any(not _valid_id(q[k][0]) for k in ("before", "after", "around") if k in q)):
            raise ValueError("Invalid Discord message-page bounds.")
        msgs = [m for m in data if isinstance(m, dict) and _valid_id(m.get("id"))]
        if len(msgs) != len(data):
            raise ValueError("Malformed Discord message-page records.")
        recs = [normalize(m) for m in msgs]
        ids = sorted(int(r["id"]) for r in recs)
        new_ids = self.store.put_messages(channel_id, recs)
        for m in msgs:
            for uid, username, gname in message_users(m):
                self.store.upsert_user(uid, username, gname)

        # Which stretch of the channel does this batch prove we now hold?
        lo = hi = None
        top = False
        now_sf = now_snowflake()
        if "before" in q:
            kind = "before"
            x = int(q["before"][0])
            top = len(ids) < limit            # short page = channel's beginning
            lo, hi = (0 if top else ids[0]), x - 1
        elif "after" in q:
            kind = "after"
            if ids:
                lo, hi = int(q["after"][0]) + 1, ids[-1]
        elif "around" in q:
            kind = "around"
            j = int(q["around"][0])
            if ids:
                top = not any(i < j for i in ids)     # nothing older exists
                lo = 0 if top else ids[0]
                hi = ids[-1] if any(i > j for i in ids) else max(ids[-1], min(j, now_sf))
            else:
                top, lo, hi = True, 0, min(j, now_sf)
        else:
            kind = "latest"
            if ids:
                top = len(ids) < limit
                lo, hi = (0 if top else ids[0]), ids[-1]
        if lo is not None and hi is not None and hi >= lo:
            self.store.add_coverage(channel_id, lo, hi)
        target = next((int(q[k][0]) for k in ("before", "after", "around") if k in q), None)
        at_start = top
        at_end = bool(ids) and (kind == "latest" or
                                (kind == "around" and target in ids and ids[-1] == target))
        if kind == "around" and ids and len(ids) < limit:
            # Discord answers "around" with about half the page on each side.
            # A page that came back short ran out of channel: the side that
            # holds clearly less than its half ends at the channel's first or
            # last message. (The same reading as "short page = beginning"
            # above; used only for the conversation check, not for coverage.)
            half = limit // 2
            at_start = at_start or sum(1 for i in ids if i < target) < half - 1
            at_end = at_end or sum(1 for i in ids if i > target) < half - 1
        elif kind == "around" and not ids:
            at_end = True                     # an empty channel has no later side
        self.fetches.append({"channel": channel_id, "kind": kind, "n": len(ids),
                             "min": ids[0] if ids else None,
                             "max": ids[-1] if ids else None,
                              "new": new_ids, "top": top, "at_start": at_start,
                              "at_end": at_end,
                              "query": {k: v[0] for k, v in q.items()},
                              "request_started": request_started,
                              "request_target": {"kind": kind, "id": target},
                              "ids": tuple(ids), "coverage_lo": lo, "coverage_hi": hi})

    def _take_search(self, guild_id, url, status, data, request_started=None):
        if not _valid_id(guild_id):
            raise ValueError("Invalid Discord server id.")
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        info = {"guild": guild_id, "query": q, "status": status, "total": None,
                "request_started": request_started,
                "hits": [], "retry_after": None, "ok": False, "deep": False}
        if isinstance(data, dict):
            info["retry_after"] = data.get("retry_after")
            if status == 200 and "messages" in data:
                info["ok"] = True
                info["total"] = data.get("total_results")
                info["deep"] = bool(data.get("doing_deep_historical_index"))
                for group in data.get("messages") or []:
                    group = [m for m in (group if isinstance(group, list) else [group])
                         if isinstance(m, dict) and _valid_id(m.get("id"))
                         and _valid_id(m.get("channel_id"))]
                    for m in group:
                        ch = str(m.get("channel_id") or "")
                        rec = normalize(m)
                        # A search hit carries less detail than the same message
                        # read from its channel, so never overwrite a full copy.
                        self.store.put_messages(ch, [rec], replace=False)
                        self.store.set_guild(ch, guild_id)
                        for uid, username, gname in message_users(m):
                            self.store.upsert_user(uid, username, gname)
                        if len(group) == 1 or m.get("hit"):
                            info["hits"].append({"channel": ch, "id": rec["id"],
                                                 "author_id": rec.get("author_id"),
                                                 "username": rec.get("username")})
        self.searches.append(info)

    async def _take_image(self, resp):
        """Keep the copy of a picture that the browser itself just loaded.

        Nothing is requested here: the bytes are read from the browser's own
        answer, so showing a picture costs Discord one request, not two."""
        ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
        parsed = _image_url(resp.url)
        if ctype not in IMAGE_EXT or parsed is None:
            return
        path = parsed.path
        if not (ATTACH_PATH_RE.match(path) or path.startswith("/external/")):
            return

        def lookup():
            """(is any message known to carry this picture?, rows still wanting it)"""
            m = ATTACH_PATH_RE.match(path)
            found = (self.store.images_by_attachment(m.group(2)) if m
                     else self.store.images_by_match(path))
            found = [row for row in found if store_mod.image_identity(row.get("url")) ==
                     store_mod.image_identity(resp.url)]
            return bool(found), [row for row in found if not row["full"]
                                 or not row.get("file") or not Path(row["file"]).is_file()]

        known, rows = lookup()
        # A picture the browser already had (or got quickly) can finish before
        # the message list that mentions it has been read and filed. Wait for
        # the lists being read right now, then look again. Nothing is held
        # meanwhile except this answer's handle.
        give_up = time.monotonic() + IMAGE_INDEX_WAIT_S
        while not known and self._filing and not self._closed:
            left = give_up - time.monotonic()
            if left <= 0:
                break
            self._filed.clear()
            try:
                await self.pacer.run(self._filed.wait(), timeout=left)
            except asyncio.TimeoutError:
                break
            known, rows = lookup()
        if not rows:
            return

        # Size first, bytes second: the limit is checked before the picture is
        # copied out of the browser, and only a few are copied at a time.
        # (The per-job download allowance is not charged here: it is for
        # pictures this app fetches itself; these the browser already has.)
        size = await self._body_size(resp)
        if size is None or size > MAX_IMAGE_BYTES:
            return
        async with self._image_reads:
            if self._closed:
                return
            _known, rows = lookup()     # another copy may have been saved meanwhile
            if not rows:
                return
            try:
                body = await self.pacer.run(resp.body(), timeout=30)
            except PacerInterrupted:
                raise
            except Exception:
                return                  # e.g. the browser no longer has it
        if not body or len(body) > MAX_IMAGE_BYTES or self._closed:
            return
        for row in rows:
            self._save_image(row, body, ctype, full=False)

    async def _body_size(self, resp):
        """How big a browser-loaded picture is, without copying it: the
        length the server declared, else what the browser measured on the
        wire. None = cannot tell, and then it is not copied."""
        declared = (resp.headers.get("content-length") or "").strip()
        if declared.isdecimal():
            return int(declared)
        if (resp.headers.get("content-encoding") or "identity").strip().lower() != "identity":
            return None                 # packed on the wire: its real size is unknown
        try:
            sizes = await self.pacer.run(resp.request.sizes(), timeout=30)
            size = int(sizes["responseBodySize"])
        except PacerInterrupted:
            raise
        except Exception:
            return None
        return size if size > 0 else None

    async def _download_image(self, url):
        """Download in a bounded, isolated transport; never render untrusted data."""
        if _image_url(url) is None or self._closed or not await self.pacer.checkpoint():
            return None
        try:
            await self.pacer.run(self._download_lock.acquire())
        except PacerInterrupted:
            return None
        cancelled = threading.Event()
        try:
            limit = min(MAX_IMAGE_BYTES, MAX_SESSION_IMAGE_BYTES - self._image_bytes)
            if limit <= 0:
                if not self._image_limit_reported:
                    self.log("NOTE: the 100 MiB image limit for this job was reached; remaining pictures were skipped.")
                    self._image_limit_reported = True
                return None
            if not _IMAGE_WORKERS.acquire(blocking=False):
                self._image_failures += 1
                if self._image_failures <= 3:
                    self.log("NOTE: image download workers are busy; this picture was skipped.")
                return None
            done = threading.Event()
            result = []
            timeout = IMAGE_DOWNLOAD_TIMEOUT_S
            remaining = self.pacer.remaining()
            if remaining is not None:
                timeout = min(timeout, remaining)
            deadline = time.monotonic() + timeout

            def worker():
                try:
                    result.append(_read_cdn_image(url, limit, deadline, cancelled))
                except Exception as exc:
                    result.append(exc)
                finally:
                    _IMAGE_WORKERS.release()
                    done.set()

            try:
                threading.Thread(target=worker, name="DiscordAnt image download",
                                 daemon=True).start()
            except Exception:
                _IMAGE_WORKERS.release()
                raise
            while not done.is_set():
                if (self._closed or time.monotonic() >= deadline
                        or not await self.pacer.nap(0.05)):
                    return None
            if isinstance(result[0], Exception):
                self._image_failures += 1
                if self._image_failures <= 3:
                    self.log(f"NOTE: skipped an image ({result[0]})")
                return None
            body, ctype = result[0]
            if self._closed or self.pacer.outcome() is not None:
                return None
            self._image_bytes += len(body)
            return body, ctype
        finally:
            cancelled.set()
            self._download_lock.release()

    def _save_image(self, row, body, ctype, full):
        if (self._closed or ctype not in IMAGE_EXT or len(body) > MAX_IMAGE_BYTES
                or not _valid_id(row.get("channel_id")) or not _valid_id(row.get("message_id"))):
            return False
        folder = self.images_dir / str(row["channel_id"])
        if not folder.resolve().is_relative_to(self.images_dir.resolve()):
            return False
        folder.mkdir(parents=True, exist_ok=True)
        ext = IMAGE_EXT[ctype]
        path = folder / (f"{row['message_id']}_{safe_name(row['key'])}"
                         f"_g{int(row['generation'])}{ext}")
        saved = self.store.save_image_file(row, path, body, full)
        if saved:
            self.images_saved += 1
        return saved

    # ---- names
    async def note_names(self, guild_id=None):
        """Read the channel and server names off the page into the archive."""
        try:
            parsed = _discord_url(self.page.url)
            m = LINK_PATH_RE.fullmatch(parsed.path) if parsed else None
            if not m:
                return
            gid, cid = m.group(1), m.group(2)
            title = await self.pacer.run(self.page.title(), timeout=10)
            parts = [p.strip() for p in title.split(" | ")]
            if len(parts) >= 3 and parts[-2].startswith("#"):
                self.store.set_name(gid, "guild", parts[-1])
                self.store.set_name(cid, "channel", parts[-2][1:], gid)
            label = await self.pacer.run(self.page.evaluate(JS_LIST_LABEL), timeout=10)
            if label and label.lower().startswith("messages in "):
                self.store.set_name(cid, "channel", label[12:], gid)
            sidebar = await self.pacer.run(self.page.evaluate(JS_SIDEBAR, guild_id or gid), timeout=10)
            for chan_id, name in sidebar:
                if chan_id.isdigit() and name:
                    self.store.set_name(chan_id, "channel", name, guild_id or gid)
        except Exception:
            pass

    # ---- full-size images
    async def fetch_full_images(self, rows):
        """Fetch raster bytes without a browser profile or rendered page."""
        rows = [r for r in rows if (not r["full"] or not r.get("file")
                                   or not Path(r["file"]).is_file())
                and _image_url(r.get("url")) and not _url_expired(r["url"])]
        if not rows or self.images_dir is None:
            return 0
        self.log(f"Fetching {len(rows)} image(s) at full size...")
        got = 0
        by_url = {}
        for row in rows:
            by_url.setdefault(_full_size_url(row), []).append(row)
        for i, (url, refs) in enumerate(by_url.items(), 1):
            if not await self.pacer.checkpoint():
                self.log(f"Stopped the image pass early ({got} of {len(rows)} done; "
                         f"the rest keep their thumbnails).")
                break
            image = await self._download_image(url)
            if self._image_limit_reported:
                break
            if image is not None:
                body, ctype = image
                for row in refs:
                    got += bool(self._save_image(row, body, ctype, full=True))
            if i % 25 == 0:
                self.log(f"  images: {i} resources of {len(by_url)}")
            if not await self.pacer.step(lo=IMAGE_MIN_S, hi=IMAGE_MAX_S):
                break
        return got


# ------------------------------------------------------------------ one job
async def run_scrape(*, guild_id, channel_id, start_dt, end_dt, out_path,
                     profile_dir, log, stop_event, store, max_minutes=None,
                     pause_event=None, fresh=False, images_dir=None,
                     full_images=True):
    """Run one channel job.

    Works out which parts of the requested range are not in the archive yet,
    scrolls through only those, then writes the whole range to out_path.

    Returns a dict: count (messages in the file), new (messages this run
    added), outcome (END_DATE, TOP, STOPPED, BUDGET or STALLED — only the
    first two mean the range is fully covered), channel_name, guild_name,
    images (files saved this run).
    """
    win_lo, win_hi = window_ids(start_dt, end_dt)
    if win_hi < win_lo:
        raise ValueError("That date range is in the future.")
    store.set_guild(channel_id, guild_id)
    if fresh:
        store.clear_coverage(channel_id, win_lo, win_hi)
        log("Starting over: this range will be scrolled through again.")

    def result(outcome, new=0, images=0):
        count = export_channel(store, channel_id, win_lo, win_hi, out_path, log)
        return {"count": count, "new": new, "outcome": outcome, "images": images,
                "channel_name": store.name(channel_id),
                "guild_name": store.name(guild_id)}

    gaps = store.gaps(channel_id, win_lo, win_hi)
    if not gaps:
        log("This whole range is already in the archive — nothing to scrape.")
        return result(TOP if win_lo == 0 else END_DATE)
    had = store.count_in(channel_id, win_lo, win_hi)
    if had:
        log(f"{had} messages of this range are already in the archive; "
            f"collecting only what is missing ({len(gaps)} stretch(es)).")

    pacer = Pacer(stop_event, pause_event, log, max_minutes)
    new_total = 0
    outcome = None
    done_reason = None

    images = 0
    try:
        async with Session(profile_dir=profile_dir, store=store, log=log, pacer=pacer,
                           images_dir=images_dir) as ses:
            page = ses.page
            if not await ses.open_app():
                return result(pacer.outcome() or STOPPED)

            floor = None            # lowest message id fetched since the last jump
            attempts = {}           # gap top -> how many times we jumped there
            need_jump = True
            named = False
            idle_steps = 0
            error_retries = 0

            def progress():
                back = snowflake_to_dt(floor).strftime("%m-%d %H:%M UTC") if floor else "?"
                return f"{new_total} new messages so far, back to {back}"

            try:
                while True:
                    got_any = False
                    fetches, ses.fetches[:] = ses.fetches[:], []
                    for f in fetches:
                        if f["channel"] != channel_id:
                            continue        # Discord preloads other channels too
                        got_any = True
                        if f["min"] is not None:
                            floor = f["min"] if floor is None else min(floor, f["min"])
                        new_total += sum(1 for i in f["new"] if win_lo <= i <= win_hi)
                    if got_any:
                        error_retries = 0

                    gaps = store.gaps(channel_id, win_lo, win_hi)
                    if not gaps:
                        if win_lo == 0:
                            done_reason, outcome = "Reached the very top of the channel.", TOP
                        else:
                            done_reason, outcome = "Reached the end date.", END_DATE
                    elif pacer.stopped:
                        done_reason, outcome = ("Stopped by user. Progress is kept — run the "
                                                "job again to carry on from here."), STOPPED
                    elif pacer.out_of_time():
                        done_reason, outcome = "Time budget reached.", BUDGET
                    if done_reason:
                        break

                    # Are we standing right above the next missing stretch? If not,
                    # jump straight to it instead of scrolling through covered ground.
                    # (A short covered stretch in between is simply scrolled through —
                    # cheaper and more natural than reloading the page.)
                    target_hi = gaps[0][1]
                    if not need_jump and floor is not None:
                        iv = store.interval_containing(channel_id, floor)
                        if not iv or iv[0] - 1 != target_hi:
                            need_jump = True
                        elif store.count_in(channel_id, iv[0], floor - 1) > SKIP_BY_JUMP_MIN:
                            need_jump = True
                    if need_jump:
                        tries = attempts.get(target_hi, 0)
                        if tries >= MAX_JUMP_TRIES:
                            done_reason, outcome = (
                                "WARNING: older history stopped loading and did not "
                                "recover — saving what was captured. This scrape is "
                                "INCOMPLETE; run it again later and it will carry on "
                                "from where it stopped.", STALLED)
                            break
                        attempts[target_hi] = tries + 1
                        when = snowflake_to_dt(target_hi)
                        log(f"Jumping to {when:%Y-%m-%d %H:%M} UTC in the channel..."
                            + (f" (try {tries + 1})" if tries else ""))
                        try:
                            await pacer.run(page.goto(f"https://discord.com/channels/{guild_id}/{channel_id}/{target_hi}"), timeout=30)
                        except PacerInterrupted as exc:
                            done_reason, outcome = "Job stopped while opening the channel.", exc.outcome
                            break
                        floor, need_jump, idle_steps = None, False, 0
                        await pacer.nap(6, 10)
                        if not named:
                            await ses.note_names(guild_id)
                            named = True
                            cname = store.name(channel_id)
                            if cname:
                                log(f"Channel: #{pretty_channel(cname)}"
                                    + (f" in {store.name(guild_id)}" if store.name(guild_id) else ""))
                        continue

                    # Discord erroring on history? Click Retry and back off rather
                    # than scrolling into a wall.
                    if not got_any and await pacer.run(page.evaluate(JS_FIND_RETRY), timeout=10):
                        error_retries += 1
                        if error_retries > MAX_ERROR_RETRIES:
                            done_reason, outcome = (
                                f"Discord kept failing to load older history after "
                                f"{MAX_ERROR_RETRIES} retries — saving what was "
                                f"captured. This scrape is INCOMPLETE.", STALLED)
                            break
                        wait = random.uniform(ERROR_BACKOFF_MIN_S, ERROR_BACKOFF_MAX_S)
                        log(f"Discord could not load older messages — clicked Retry, "
                            f"waiting {wait:.0f}s (attempt {error_retries})")
                        await pacer.nap(wait)
                        continue  # don't count this as an idle scroll step

                    info = await pacer.run(page.evaluate(JS_SCROLLER), timeout=10)
                    idle_steps = 0 if got_any else idle_steps + 1
                    if not info:
                        log("WARNING: message list not found (channel still loading?)")
                        idle_steps += 3
                    else:
                        await pacer.run(page.keyboard.press("PageUp"), timeout=10)
                    if idle_steps >= IDLE_STEPS_LIMIT:
                        # Nothing new is arriving. Jump back to the edge of what is
                        # missing: that either proves this is the channel's beginning
                        # or gives a stuck page a fresh start.
                        need_jump = True
                        continue
                    await pacer.step(progress)

            except PacerInterrupted as exc:
                done_reason, outcome = "Job stopped while waiting for Discord.", exc.outcome
            except asyncio.TimeoutError:
                done_reason = "Discord did not answer before the operation timeout. Progress is kept."
                outcome = pacer.outcome() or STALLED

            log(done_reason)
            images = ses.images_saved
            if images_dir is not None and full_images and outcome in (END_DATE, TOP, STALLED):
                await ses.fetch_full_images(store.images_in(channel_id, win_lo, win_hi))
                images = ses.images_saved
                if pacer.outcome() is not None:
                    outcome = pacer.outcome()
            if images:
                log(f"Images saved this run: {images}")

    except PacerInterrupted as exc:
        outcome = exc.outcome
    except asyncio.TimeoutError:
        log("Discord browser startup or navigation timed out. Progress is kept.")
        outcome = pacer.outcome() or STALLED

    return result(outcome or STALLED, new_total, images)
