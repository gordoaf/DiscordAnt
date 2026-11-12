"""User history: everything one person posted in a server (inside a date
range), together with the conversation around each message.

How it works, all inside the same real browser window at human pace:

  1. SEARCH — types `from:<user>` (plus dates) into Discord's own search bar
     and pages through the results with the Next button, reading the answers
     Discord's client receives. That finds every message of theirs in every
     channel of the server the account can see.
  2. CONVERSATIONS — for each message whose surroundings are not in the
     archive yet, jumps to it (as clicking "Jump" on a search result does), so
     Discord loads the messages around it. What a message replied to is kept
     as a short quote (author and the first few hundred characters); the
     replied-to message itself is in the output only when it is close by.
  3. Writes a JSON file grouped by channel, and a readable .md transcript.

Both steps remember their progress in the archive, so a stopped job carries on
where it left off and nothing is looked up twice.
"""

import html
import json
import random
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from . import scraper_core as sc
    from . import exports
except ImportError:  # running as a plain script / frozen exe
    import scraper_core as sc
    import exports

SEARCH_PAGE = 25                       # results per page of Discord's search
TYPE_MIN_MS, TYPE_MAX_MS = 130, 170    # per key, when typing into the search bar
PAGE_MIN_S, PAGE_MAX_S = 4.0, 8.0      # between result pages
JUMP_MIN_S, JUMP_MAX_S = 4.0, 8.0      # between conversation look-ups
CONTEXT_K = 8                          # messages of conversation kept each side
DATE_PAD_DAYS = 2                      # search a little wider, filter exactly
# Discord's search index trails real time a little; never claim the last few
# minutes as "searched".
SEARCH_NOW_MARGIN_S = 15 * 60
MAX_SEARCH_PASSES = 30
MAX_SHORT_PASSES = 2                   # passes in a row cut short before giving up
MAX_JUMP_FAILS = 5
# How long to wait for Discord's client to answer, in seconds.
ENTER_WAIT_S = 7                       # after pressing Enter on the search
SEARCH_WAIT_S = 40                     # for a page of search results
JUMP_WAIT_S = 7                        # after an in-app jump to a message
GOTO_WAIT_S = 15                       # after opening a message's link
MAX_SEARCH_WAIT_S = 120               # a retrying client must not wait forever

JS_OPTIONS = """() => [...document.querySelectorAll('[role="listbox"] [role="option"]')]
    .map(o => ({sel: o.getAttribute('aria-selected'),
                user_id: o.getAttribute('data-user-id') ||
                    (o.querySelector('[data-user-id]') || {getAttribute() { return null; }})
                        .getAttribute('data-user-id') ||
                    (((o.querySelector('img[src]') || {}).src || '').match(/\\/avatars\\/(\\d+)\\//) || [])[1] ||
                    (((o.querySelector('a[href*="/users/"]') || {}).href || '').match(/\\/users\\/(\\d+)/) || [])[1],
                text: (o.innerText || '').trim().slice(0, 80),
                user: (o.querySelector('[role="img"][aria-label]') || {getAttribute() { return null; }})
                      .getAttribute('aria-label')}))"""

JS_NEXT_READY = """() => {
    const b = document.querySelector('button[rel="next"]');
    return !!b && !b.disabled && b.getAttribute('aria-disabled') !== 'true';
}"""

JS_NEWEST = """() => {
    for (const el of document.querySelectorAll('button, [role="tab"]')) {
        if (el.closest('[data-list-id="chat-messages"]')) continue;
        if (/^(new|newest|most recent)$/i.test((el.innerText || '').trim())) {
            el.click();
            return true;
        }
    }
    return false;
}"""

JS_JUMP = """(p) => {
    history.pushState({}, '', p);
    window.dispatchEvent(new PopStateEvent('popstate', {state: {}}));
}"""


class SearchProblem(Exception):
    """The search could not be set up (shown to the user as the job's note)."""


def user_key(guild_id, author_id):
    """Archive key under which 'this stretch has been searched' is recorded."""
    # Older versions did not validate search sort/offsets. Preserve those
    # records, but never reuse their unproved coverage as a completion claim.
    return f"user:v2:{guild_id}:{author_id}"


def search_now():
    return sc.dt_to_snowflake(datetime.now(timezone.utc)
                              - timedelta(seconds=SEARCH_NOW_MARGIN_S))


# ------------------------------------------------------------------ search
async def _type(page, text, pacer):
    await pacer.run(page.keyboard.type(text, delay=random.randint(TYPE_MIN_MS, TYPE_MAX_MS)))


async def _await_search(ses, n0, timeout=None, matches=None):
    """Wait for a usable search answer that arrived after index n0."""
    t_end = time.monotonic() + (SEARCH_WAIT_S if timeout is None else timeout)
    hard_end = time.monotonic() + MAX_SEARCH_WAIT_S
    seen = n0
    while time.monotonic() < t_end:
        paused = time.monotonic()
        if not await ses.pacer.checkpoint():
            return None
        waited = time.monotonic() - paused
        t_end += waited
        hard_end += waited
        while len(ses.searches) > seen:
            entry = ses.searches[seen]
            seen += 1
            if matches is not None and not matches(entry):
                continue
            if entry["ok"]:
                return entry
            # "still indexing" / "slow down": Discord's client retries by itself
            try:
                wait = max(0, min(float(entry.get("retry_after") or 5), MAX_SEARCH_WAIT_S))
            except (TypeError, ValueError, OverflowError):
                wait = 5
            t_end = min(hard_end, max(t_end, time.monotonic() + wait + 25))
        if not await ses.pacer.nap(0.25):
            return None
    return None


def _query_signature(entry, guild_id, *, first=False):
    """Reject filters/order that cannot prove complete newest-first coverage."""
    if str(entry.get("guild")) != str(guild_id):
        raise SearchProblem("The search answered for a different server.")
    q = entry.get("query") or {}
    allowed = {"author_id", "min_id", "max_id", "sort_by", "sort_order", "offset"}
    if set(q) - allowed or not str(q.get("author_id") or "").isdigit():
        raise SearchProblem("The search must be server-wide, from one user, with no extra filters.")
    if q.get("sort_by") != "timestamp" or q.get("sort_order") != "desc":
        raise SearchProblem("Search order is unverified. Select Newest in Discord's search and try again.")
    try:
        offset = int(q.get("offset") or 0)
        qmin = int(q.get("min_id") or 0)
        qmax = int(q["max_id"]) if q.get("max_id") else None
    except (TypeError, ValueError):
        raise SearchProblem("Could not read the search's range or page offset.")
    if offset < 0 or qmin < 0 or (qmax is not None and qmax <= qmin) or (first and offset != 0):
        raise SearchProblem("The search answered with an unexpected range or page offset.")
    return tuple(sorted((k, str(v)) for k, v in q.items() if k != "offset")), offset, qmin, qmax


def _response_matches(entry, guild_id, triggered, name, author_id=None, lo=0, hi=None):
    """Require a request created by this action, and independent identity proof."""
    try:
        if (str(entry.get("guild")) != str(guild_id)
                or float(entry.get("request_started", -1)) < triggered):
            return False
        q = entry.get("query") or {}
        candidate = str(q.get("author_id") or "")
        qmin = int(q.get("min_id") or 0)
        qmax = int(q["max_id"]) if q.get("max_id") else None
    except (TypeError, ValueError):
        return False
    if not candidate.isdigit() or qmin > lo or (qmax is not None and hi is not None and qmax <= hi):
        return False
    if author_id is not None:
        return candidate == str(author_id)
    hits = entry.get("hits") or []
    return bool(hits) and all(str(h.get("author_id")) == candidate
                              and (h.get("username") or "").casefold() == name.casefold() for h in hits)


async def _start_search(ses, name, lo, hi, log, guild_id, author_id=None):
    """Type the query for one stretch [lo, hi] and run it. Returns the first
    page's answer."""
    page, pacer = ses.page, ses.pacer
    await pacer.run(page.bring_to_front())
    await pacer.run(page.keyboard.press("Control+f"))
    await pacer.nap(1.0, 1.6)
    await pacer.run(page.keyboard.press("Control+a"))
    await pacer.nap(0.3, 0.6)
    await pacer.run(page.keyboard.press("Backspace"))
    await pacer.nap(0.8, 1.2)

    if lo > 0:
        day = (sc.snowflake_to_dt(lo) - timedelta(days=DATE_PAD_DAYS)).date()
        await _type(page, "after:", pacer)
        await pacer.nap(0.5, 0.9)
        await _type(page, f"{day.isoformat()} ", pacer)
        await pacer.nap(0.8, 1.2)
    top = sc.snowflake_to_dt(hi) + timedelta(days=DATE_PAD_DAYS)
    if top < datetime.now(timezone.utc):
        await _type(page, "before:", pacer)
        await pacer.nap(0.5, 0.9)
        await _type(page, f"{top.date().isoformat()} ", pacer)
        await pacer.nap(0.8, 1.2)
    await _type(page, "from:", pacer)
    await pacer.nap(0.5, 0.9)
    await _type(page, name, pacer)
    await pacer.nap(1.8, 2.6)
    if not await pacer.checkpoint():
        return None

    # Pick the suggestion that is exactly this user.
    options = await pacer.run(page.evaluate(JS_OPTIONS))
    want = name.lower()
    idx = next((i for i, o in enumerate(options)
                if (o.get("user") or "").lower() == want), None)
    if idx is None:
        idx = next((i for i, o in enumerate(options) if o.get("user")
                    and (o.get("text") or "").split("\n")[0].strip().lower() == want), None)
    if idx is None:
        seen = [o["user"] for o in options if o.get("user")]
        await pacer.run(page.keyboard.press("Escape"))
        hint = ("Discord suggested: " + ", ".join(seen[:8])) if seen else \
               "Discord suggested nobody for that name."
        raise SearchProblem(f"No user called '{name}' was found in this server. {hint} "
                            f"(Use the exact username, not the display name.)")
    cur = next((i for i, o in enumerate(options) if o.get("sel") == "true"), 0)
    suggested_id = options[idx].get("user_id")
    if suggested_id and not str(suggested_id).isdigit():
        suggested_id = None
    if suggested_id and author_id and str(suggested_id) != str(author_id):
        raise SearchProblem("This username now identifies a different account; verify it before collecting.")
    expected_author = suggested_id or author_id
    for _ in range(abs(idx - cur)):
        await pacer.run(page.keyboard.press("ArrowDown" if idx > cur else "ArrowUp"))
        await pacer.nap(0.2, 0.4)

    n0 = len(ses.searches)
    triggered = time.monotonic()
    matches = lambda e: _response_matches(e, guild_id, triggered, name, expected_author, lo, hi)
    await pacer.run(page.keyboard.press("Enter"))
    entry = await _await_search(ses, n0, timeout=ENTER_WAIT_S, matches=matches)
    if entry is None and await pacer.checkpoint() and len(ses.searches) == n0:
        triggered = time.monotonic()
        await pacer.run(page.keyboard.press("Enter"))  # first Enter picked the user
        entry = await _await_search(ses, n0, matches=matches)
    elif entry is None and await pacer.checkpoint():
        entry = await _await_search(ses, n0, matches=matches)
    if entry is None:
        if not await pacer.checkpoint():
            return None
        raise SearchProblem("Discord did not return a newly requested search with a verified user identity. "
                            "Try again; for an unknown user with no results, collect a channel containing their messages first.")
    q = entry["query"]
    if "author_id" not in q or "channel_id" in q:
        await pacer.run(page.keyboard.press("Escape"))
        raise SearchProblem("The search did not come out as a server-wide 'from user' "
                            "search — Discord's search box may have changed. Nothing "
                            "was collected.")
    if q.get("sort_by") != "timestamp" or q.get("sort_order") != "desc":
        # Drive only the client's visible sort control. If the layout or
        # defaults change, fail closed instead of fabricating coverage.
        n0 = len(ses.searches)
        initial_author = q["author_id"]
        triggered = time.monotonic()
        if await pacer.run(page.evaluate(JS_NEWEST)):
            sorted_entry = await _await_search(ses, n0, matches=lambda e:
                _response_matches(e, guild_id, triggered, name, initial_author, lo, hi))
            if sorted_entry is not None:
                entry = sorted_entry
    _query_signature(entry, guild_id, first=True)
    return entry


async def _search_pass(ses, store, guild_id, name, lo, hi, log):
    """Search one stretch and page through it, newest to oldest.

    Returns {"author_id", "hits", "pages", "cut_short", "problem"}.
    """
    page, pacer = ses.page, ses.pacer
    out = {"author_id": None, "hits": 0, "pages": 0, "cut_short": False,
           "problem": None}
    known_id = store.find_user(name)
    known = store.user(known_id) if known_id else None
    if not known or (known[1] or "").casefold() != name.casefold():
        known_id = None
    entry = await _start_search(ses, name, lo, hi, log, guild_id, known_id)
    if entry is None:
        return out
    signature, offset, qmin, qmax = _query_signature(entry, guild_id, first=True)
    q = entry["query"]
    author_id = out["author_id"] = q["author_id"]
    # min_id/max_id are exclusive API bounds. Never claim beyond the exact
    # requested gap, even when the UI's date query was deliberately padded.
    cov_hi = min(hi, search_now(), qmax - 1 if qmax is not None else hi)
    cov_floor = max(lo, qmin + 1 if qmin else 0)

    oldest = None
    reached_end = False
    deep = False
    previous_low = None
    seen_ids = set()
    expected_total = None
    while True:
        deep = deep or entry["deep"]
        try:
            total = int(entry["total"])
        except (TypeError, ValueError):
            raise SearchProblem("Search did not report a valid result count.")
        if total < 0:
            raise SearchProblem("Search reported an invalid result count.")
        hits = entry["hits"]
        wrong = [h for h in hits if str(h.get("author_id")) != str(author_id)]
        if wrong:
            raise SearchProblem("The search returned other people's messages — "
                                "stopped so nothing wrong is saved.")
        try:
            ids = [int(h["id"]) for h in hits]
        except (TypeError, ValueError, KeyError):
            raise SearchProblem("Search returned an invalid message ID.")
        if (ids != sorted(ids, reverse=True) or len(set(ids)) != len(ids)
                or any(i <= qmin or (qmax is not None and i >= qmax) for i in ids)):
            raise SearchProblem("A search page was not in newest-first order or left "
                                "the requested range; coverage was not recorded.")
        # The results can move while they are being paged: the person posts,
        # or a message is deleted, and every later page shifts by one. The
        # pages read before that are still good. Keep them, drop this page,
        # and let the caller search again from where the good pages ended.
        if ((expected_total is not None and total != expected_total)
                or seen_ids.intersection(ids)
                or (previous_low is not None and ids and max(ids) >= previous_low)):
            log("The search results moved while paging (a new or deleted "
                "message). Searching again from the last good page.")
            out["cut_short"] = True
            break
        expected_total = total
        out["hits"] += len(hits)
        out["pages"] += 1
        seen_ids.update(ids)
        if ids:
            low = min(ids)
            previous_low = low
            oldest = low if oldest is None else min(oldest, low)
        if offset == 0:
            log(f"Search found {total} message(s) in this stretch"
                + (" (Discord is still indexing old messages, so this may be "
                   "incomplete)" if deep else "") + ".")
        if offset + len(hits) >= total:
            reached_end = True
            break
        if len(hits) != SEARCH_PAGE:
            # Fewer results than Discord's own count promised. What came is
            # kept; the rest of the stretch is searched again by the caller
            # and stays marked as not searched until a search covers it.
            out["cut_short"] = True
            break
        if oldest is not None and oldest < lo:
            break                        # paged past everything we came for
        if pacer.stopped or pacer.out_of_time():
            break
        if not await pacer.step(lambda: f"search page {offset // SEARCH_PAGE + 1} of "
                                        f"{-(-total // SEARCH_PAGE)}",
                                lo=PAGE_MIN_S, hi=PAGE_MAX_S):
            break
        nxt = None
        for attempt in range(2):
            if not await pacer.run(page.evaluate(JS_NEXT_READY)):
                break
            n0 = len(ses.searches)
            triggered = time.monotonic()
            try:
                await pacer.run(page.click('button[rel="next"]', timeout=5000))
            except sc.PacerInterrupted:
                raise
            except Exception:
                break
            # Other in-flight searches must not become this pass's next page.
            nxt = await _await_search(ses, n0, matches=lambda e:
                _response_matches(e, guild_id, triggered, name, author_id, lo, hi))
            if nxt is not None or pacer.stopped:
                break
            await pacer.nap(10, 20)
        if nxt is None:
            # No further page came. What was read so far is kept; the caller
            # searches again from there (Discord also caps how deep one search
            # can be paged, so a very long history takes several searches).
            out["cut_short"] = not pacer.stopped
            break
        next_signature, next_offset, _qmin, _qmax = _query_signature(nxt, guild_id)
        if next_signature != signature or next_offset != offset + SEARCH_PAGE:
            raise SearchProblem("The search query or page offset changed; coverage was not recorded.")
        offset = next_offset
        entry = nxt

    # Record which stretch is now known to be fully searched.
    if deep:
        out["problem"] = "Discord is still indexing this server's old messages for search"
    else:
        cov_lo = cov_floor if reached_end else max(cov_floor, oldest) if oldest is not None else None
        if cov_lo is not None and cov_hi >= cov_lo:
            store.add_coverage(user_key(guild_id, author_id), cov_lo, cov_hi)
    return out


# ------------------------------------------------------------------ context
def user_hits(store, guild_id, author_id, lo, hi):
    """[(channel_id, rec)] — this user's archived messages in this server."""
    return [(ch, rec) for ch, rec in store.by_author(author_id, lo, hi)
            if store.guild_of(ch) == str(guild_id)]


def needs_context(store, ch, mid, k):
    return (store.context_refresh_pending(ch, mid)
            or not (store.has_context(ch, mid, k) or store.context_marked(ch, mid, k)))


def _context_proof(fetch, ch, mid, k, triggered=None):
    """A same-channel response alone says nothing about the intended target."""
    q = fetch.get("query") or {}
    try:
        if triggered is not None and float(fetch.get("request_started", -1)) < triggered:
            return None
        ids = sorted(set(int(i) for i in fetch.get("ids", ())))
        target = int(q.get("around") or -1)
    except (TypeError, ValueError):
        return None
    if str(fetch.get("channel")) != str(ch) or target != int(mid):
        return None
    # The answer to "around this message" is used even when the message itself
    # is missing from it: it was deleted since it was found, and what Discord
    # sent is everything that still surrounds that spot.
    before = sum(i < int(mid) for i in ids)
    after = sum(i > int(mid) for i in ids)
    # The channel-edge flags come from the captured answer itself (see
    # Session._take_batch), never from a handful of cached search hits.
    at_start = bool(fetch.get("at_start"))
    at_end = bool(fetch.get("at_end"))
    return {"before": before, "after": after, "at_start": at_start,
            "at_end": at_end, "gone": int(mid) not in ids,
            "complete": (before >= k or at_start) and (after >= k or at_end)}


async def _wait_fetch(ses, ch, mid, n0, timeout, k=CONTEXT_K, triggered=None):
    t_end = time.monotonic() + timeout
    while time.monotonic() < t_end:
        paused = time.monotonic()
        if not await ses.pacer.checkpoint():
            return None
        t_end += time.monotonic() - paused
        for f in ses.fetches[n0:]:
            proof = _context_proof(f, ch, mid, k, triggered)
            if proof is not None:
                return proof
        if not await ses.pacer.nap(0.25):
            return None
    return None


async def _load_context(ses, guild_id, ch, mid, k=CONTEXT_K):
    """Jump to one message so Discord loads the conversation around it."""
    path = f"/channels/{guild_id}/{ch}/{mid}"
    n0 = len(ses.fetches)
    triggered = time.monotonic()
    try:
        await ses.pacer.run(ses.page.evaluate(JS_JUMP, path))
    except sc.PacerInterrupted:
        raise
    except Exception:
        pass
    proof = await _wait_fetch(ses, ch, mid, n0, JUMP_WAIT_S, k, triggered)
    if proof is not None:
        return proof
    if not await ses.pacer.checkpoint():
        return None
    # Discord already had this spot loaded (or ignored the jump): open the
    # message's link directly instead.
    n0 = len(ses.fetches)
    triggered = time.monotonic()
    await ses.pacer.run(ses.page.goto("https://discord.com" + path))
    return await _wait_fetch(ses, ch, mid, n0, GOTO_WAIT_S, k, triggered)


# ------------------------------------------------------------------ output
def channel_label(store, ch):
    name = store.name(ch)
    return sc.pretty_channel(name) if name else f"channel {str(ch)[-4:]}"


def build_conversations(store, ch, hit_ids, k):
    """Group a channel's hits, each with k messages either side, into runs of
    consecutive messages. Returns a list of lists of archive records."""
    windows = []
    for mid in sorted(hit_ids):
        iv = store.interval_containing(ch, mid)
        ids = [mid]
        if iv and k > 0:
            before, after = store.neighbours(ch, mid, k)
            ids = ([int(r["id"]) for r in before if int(r["id"]) >= iv[0]] + [mid]
                   + [int(r["id"]) for r in after if int(r["id"]) <= iv[1]])
        windows.append((ids, iv))
    runs = []
    for ids, iv in windows:
        if runs:
            last_ids, last_iv = runs[-1]
            joins = ids[0] <= last_ids[-1]
            if not joins and iv and iv == last_iv:
                nxt = store.neighbours(ch, last_ids[-1], 1)[1]
                joins = bool(nxt) and int(nxt[0]["id"]) == ids[0]
            if joins:
                runs[-1] = (sorted(set(last_ids) | set(ids)), iv)
                continue
        runs.append((ids, iv))
    return [[r for r in (store.get(ch, i) for i in ids) if r] for ids, _iv in runs]


def _md_escape(text):
    return str(text or "").replace("\r", "").replace("\n", "\n        ")


def _md_inline(text):
    """Untrusted names and notes stay literal in headings and prose."""
    text = html.escape(" ".join(str(text or "").split()), quote=False)
    return re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", text)


def _single_line(text):
    return " ".join(str(text or "").split())


def _md_line(row):
    mark = "▶" if row.get("theirs") else " "
    head = f"{mark} {_single_line(row['ts'][11:16])} {_single_line(row['author'])}"
    if row.get("reply_to"):
        head += f" (↩ {_single_line(row['reply_to'])})"
    parts = [f"{head}: {_md_escape(row.get('content'))}".rstrip()]
    if row.get("forwarded"):
        parts.append(f"        [forwarded] {_md_escape(row['forwarded'])}")
    for e in row.get("embeds") or []:
        parts.append(f"        [link/embed] {_md_escape(e)}")
    shown = {im.get("name") for im in row.get("images") or []}
    for im in row.get("images") or []:
        parts.append(f"        [image {_single_line(im.get('name'))}] {_single_line(im.get('file'))}".rstrip())
    others = [a for a in row.get("attachments") or [] if a not in shown]
    if others:
        parts.append("        [files] " + ", ".join(_single_line(a) for a in others))
    if row.get("poll"):
        parts.append(f"        [poll] {_md_escape(row['poll'])}")
    if row.get("stickers"):
        parts.append("        [sticker] " + ", ".join(_single_line(s) for s in row["stickers"]))
    if row.get("reactions"):
        parts.append(f"        [reactions] {_single_line(row['reactions'])}")
    return "\n".join(parts)


def export_user(store, guild_id, author_id, lo, hi, out_path, log,
                k=CONTEXT_K, note=None, start_dt=None, end_dt=None,
                completeness=None):
    """Write the JSON (grouped by channel) and the .md transcript."""
    hits = user_hits(store, guild_id, author_id, lo, hi)
    by_ch = {}
    for ch, rec in hits:
        by_ch.setdefault(ch, set()).add(int(rec["id"]))
    urow = store.user(author_id)
    username = (urow[1] if urow else None) or str(author_id)
    display = (urow[2] if urow else None) or username
    server = store.name(guild_id) or f"server {str(guild_id)[-4:]}"

    channels = []
    for ch, ids in sorted(by_ch.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        convs = []
        for recs in build_conversations(store, ch, ids, k):
            rows = []
            for rec in recs:
                row = sc.export_row(rec, ch, store)
                if int(rec["id"]) in ids:
                    row["theirs"] = True
                rows.append(row)
            convs.append(rows)
        channels.append({"channel": channel_label(store, ch), "channel_id": ch,
                         "messages": len(ids), "conversations": convs})

    doc = {
        "user": username, "display_name": display, "user_id": str(author_id),
        "server": server, "server_id": str(guild_id),
        "from": end_dt.strftime("%Y-%m-%d") if end_dt else "beginning",
        "to": start_dt.strftime("%Y-%m-%d") if start_dt else "now",
        "messages": len(hits), "channel_count": len(channels),
        "how_to_read": ("Grouped by channel, then into conversations. Rows with "
                        "\"theirs\": true were posted by this user; the other rows "
                        "are the conversation around them."),
    }
    if note:
        doc["note"] = note
    if completeness is not None:
        doc.update(completeness)
    doc["channels"] = channels
    out_path = Path(out_path)
    md = [f"# {_md_inline(username)}" + (f" ({_md_inline(display)})" if display != username else "")
          + f" in {_md_inline(server)}",
          f"{doc['from']} to {doc['to']} · {len(hits)} messages in "
          f"{len(channels)} channel(s)", ""]
    if note:
        md += [f"**Note:** {_md_inline(note)}", ""]
    if completeness is not None:
        through = completeness.get("searched_through")
        if completeness.get("complete"):
            md += ["Complete for these dates.", ""]
        elif completeness.get("deferred_recent") and through:
            md += [f"Complete up to {_md_inline(through)}. Newer messages are "
                   f"collected by the next run.", ""]
        else:
            md += ["Not complete yet: run the job again to continue. Searched through: "
                   f"{_md_inline(through or 'not yet verified')}.", ""]
    md += [f"Lines marked ▶ are {_md_inline(username)}'s. Unmarked lines are the conversation "
           f"around them. Times are UTC.", ""]
    for c in channels:
        md += [f"## #{_md_inline(c['channel'])} — {c['messages']} message(s)", ""]
        for conv in c["conversations"]:
            day = None
            transcript = []
            for row in conv:
                if row["ts"][:10] != day:
                    day = row["ts"][:10]
                    transcript.append(f"--- {_single_line(day)} ---")
                transcript.append(_md_line(row))
            text = "\n".join(transcript)
            fence = "`" * max(3, 1 + max((len(m.group()) for m in re.finditer(r"`+", text)), default=0))
            md += [fence, text, fence, ""]
    md_path = out_path.with_suffix(".md")
    exports.check_paths([out_path, md_path])
    exports.write_text(out_path, json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    exports.write_text(md_path, "\n".join(md), encoding="utf-8")

    log(f"Saved {len(hits)} messages by {username} across {len(channels)} channel(s).")
    log(f"Output: {out_path}")
    log(f"Readable transcript: {md_path}")
    return len(hits), len(channels)


# ------------------------------------------------------------------ one job
async def run_user_history(*, guild_id, channel_id, username, start_dt, end_dt,
                           out_path, profile_dir, log, stop_event, store,
                           max_minutes=None, pause_event=None, fresh=False,
                           images_dir=None, full_images=True, context_k=CONTEXT_K):
    """Collect a verified range; deferred index lag remains explicitly incomplete."""
    name = (username or "").strip().lstrip("@")
    if not name:
        raise ValueError("Enter a username.")
    win_lo, win_hi = sc.window_ids(start_dt, end_dt)
    if win_hi < win_lo:
        raise ValueError("That date range is in the future.")
    search_hi = min(win_hi, search_now())
    deferred = search_hi < win_hi
    store.set_guild(channel_id, guild_id)
    author_id = store.find_user(name)
    known = store.user(author_id) if author_id else None
    if not known or (known[1] or "").lower() != name.lower():
        author_id = None
    queued_refresh = set()

    def queue_refresh():
        if fresh and author_id:
            for ch, rec in user_hits(store, guild_id, author_id, win_lo, win_hi):
                target = (ch, int(rec["id"]))
                if target not in queued_refresh:
                    store.request_context_refresh(*target)
                    queued_refresh.add(target)

    if fresh and author_id:
        # Start over is asked for once (the queue has already used up its
        # flag), so what it means is written into the archive before anything
        # can be stopped: first every message of theirs already held is marked
        # "read again in full", then the search record is cleared. A Stop, a
        # time limit or a crash during the first search then loses nothing;
        # the next ordinary run finds both and carries on.
        queue_refresh()
        store.clear_coverage(user_key(guild_id, author_id), win_lo, win_hi)
        log("Starting over: search results and full messages will be refreshed.")

    def search_gaps():
        if search_hi < win_lo:
            return []
        if not author_id:
            return [(win_lo, search_hi)]
        return store.gaps(user_key(guild_id, author_id), win_lo, search_hi)

    def context_todo():
        if not author_id:
            return []
        queue_refresh()
        todo = [(ch, int(rec["id"])) for ch, rec in
                user_hits(store, guild_id, author_id, win_lo, win_hi)
                if (store.context_refresh_pending(ch, int(rec["id"]))
                    or (context_k > 0 and needs_context(store, ch, int(rec["id"]), context_k)))]
        return sorted(todo, key=lambda x: (x[0], -x[1]))

    def finish(outcome, note=None, images=0):
        eligible_complete = not search_gaps()
        searched_any = search_hi >= win_lo
        if deferred and not searched_any:
            # The whole range is newer than Discord's search can see yet, so
            # nothing was searched: the job has to come back later.
            tail_note = ("These dates are too recent for Discord's search, which runs "
                         "about 15 minutes behind. Run this job again later.")
            note = (note + "; " if note else "") + tail_note
            if outcome == sc.END_DATE:
                outcome = sc.STALLED
        elif deferred:
            # A job "up to now" is finished once everything the search can see
            # is collected. The last few minutes belong to the next run; the
            # output says exactly where this one stopped.
            tail_note = ("Searched up to "
                         + sc.snowflake_to_dt(search_hi).strftime("%Y-%m-%d %H:%M UTC")
                         + ". Discord's search runs about 15 minutes behind; anything "
                         "newer is collected the next time this runs.")
            note = (note + "; " if note else "") + tail_note
        metadata = {
            "complete": outcome == sc.END_DATE and not deferred,
            "searched_any": searched_any,
            "deferred_recent": deferred,
            "searched_through": (sc.snowflake_to_dt(search_hi).isoformat()
                                 if eligible_complete and search_hi >= win_lo else None),
            "search_cutoff": sc.snowflake_to_dt(search_hi).isoformat(),
            "requested_through": sc.snowflake_to_dt(win_hi).isoformat(),
        }
        if note:
            log("NOTE: " + note)
        if not author_id:
            return dict(count=0, channels=0, outcome=outcome, username=name,
                        author_id=None, guild_name=store.name(guild_id),
                        images=images, note=note, **metadata)
        count, nch = export_user(store, guild_id, author_id, win_lo, win_hi, out_path,
                                 log, k=context_k, note=note, start_dt=start_dt,
                                 end_dt=end_dt, completeness=metadata)
        urow = store.user(author_id)
        return dict(count=count, channels=nch, outcome=outcome,
                    username=(urow[1] if urow else None) or name,
                    author_id=author_id, guild_name=store.name(guild_id),
                    images=images, note=note, **metadata)

    if not search_gaps() and not context_todo():
        return finish(sc.END_DATE)

    pacer = sc.Pacer(stop_event, pause_event, log, max_minutes)
    outcome = None
    note = None
    images = 0

    async def collect(ses):
        nonlocal author_id, note, outcome, images
        if not await ses.open_app():
            outcome = pacer.outcome() or sc.STOPPED
            return
        await pacer.run(ses.page.goto(f"https://discord.com/channels/{guild_id}/{channel_id}"))
        if not await pacer.nap(6, 10):
            outcome = pacer.outcome()
            return
        await ses.note_names(guild_id)
        passes = short_passes = 0
        while await pacer.checkpoint():
            gaps = search_gaps()
            if not gaps:
                break
            passes += 1
            if passes > MAX_SEARCH_PASSES:
                note, outcome = "the search needed too many passes; some messages may be missing", sc.STALLED
                break
            lo, hi = gaps[0]
            log(f"Searching the server for messages from {name}...")
            try:
                res = await _search_pass(ses, store, guild_id, name, lo, hi, log)
            except SearchProblem as e:
                note, outcome = str(e), sc.STALLED
                break
            if res["author_id"]:
                if author_id and res["author_id"] != author_id:
                    note = f"'{name}' now matches a different account. Stopped; verify the exact username."
                    outcome = sc.STALLED
                    break
                author_id = res["author_id"]
                # A verified zero-hit search has no message author record to
                # populate the user table, but its selected identity is known.
                store.upsert_user(author_id, name)
            queue_refresh()
            if res["problem"]:
                note, outcome = res["problem"], sc.STALLED
                break
            if not await pacer.checkpoint() or not author_id:
                break
            short = res["cut_short"] and res["pages"] < 200
            if short or search_gaps() == gaps:
                short_passes += 1
                if short_passes > MAX_SHORT_PASSES or search_gaps() == gaps:
                    note, outcome = "the search results stopped loading part-way", sc.STALLED
                    break
                if not await pacer.nap(15, 30):
                    break
            else:
                short_passes = 0
            if not await pacer.nap(2, 4):
                break
        if await pacer.checkpoint():
            await pacer.run(ses.page.keyboard.press("Escape"))
        if author_id and await pacer.checkpoint():
            todo = context_todo()
            fails = done = 0
            seen_channels = set()
            for ch, mid in todo:
                if not await pacer.checkpoint():
                    break
                if not needs_context(store, ch, mid, context_k):
                    continue
                proof = await _load_context(ses, guild_id, ch, mid, context_k)
                del ses.fetches[:]
                # A correlated full copy can use already-verified neighbouring
                # archive messages, but never an unrelated response as proof.
                if proof is not None and store.has_context(ch, mid, context_k):
                    proof = dict(proof, before=max(context_k, proof["before"]),
                                 after=max(context_k, proof["after"]), complete=True)
                if proof is not None and proof["complete"] and store.mark_context(
                        ch, mid, context_k, before=proof["before"], after=proof["after"],
                        at_start=proof["at_start"], at_end=proof["at_end"]):
                    fails = 0
                    done += 1
                else:
                    fails += 1
                    if fails >= MAX_JUMP_FAILS:
                        note = note or "conversations stopped loading with verified context"
                        outcome = sc.STALLED
                        break
                if ch not in seen_channels:
                    seen_channels.add(ch)
                    if not await pacer.nap(1, 2):
                        break
                    await ses.note_names(guild_id)
                if not await pacer.step(lambda: f"{done} conversation(s) loaded",
                                        lo=JUMP_MIN_S, hi=JUMP_MAX_S):
                    break
        if pacer.outcome() is not None:
            outcome = pacer.outcome()
        elif outcome is None:
            outcome = sc.STALLED if search_gaps() or context_todo() else sc.END_DATE
        if outcome == sc.STALLED and note is None:
            note = "Some search results or conversation context remain unverified; run again to continue."
        if (author_id and images_dir is not None and full_images
                and outcome in (sc.END_DATE, sc.STALLED)):
            rows = []
            for ch, rec in user_hits(store, guild_id, author_id, win_lo, win_hi):
                rows += store.images_for(ch, rec["id"])
            await ses.fetch_full_images(rows)
        images = ses.images_saved
        if ses.image_note():
            log(ses.image_note())
        if pacer.outcome() is not None:
            outcome = pacer.outcome()

    try:
        async with sc.Session(profile_dir=profile_dir, store=store, log=log, pacer=pacer,
                              images_dir=images_dir) as ses:
            await collect(ses)
    except sc.PacerInterrupted as exc:
        outcome = exc.outcome
    if (not author_id and note is None and outcome not in (sc.STOPPED, sc.BUDGET)
            and search_hi >= win_lo):
        note = f"no user called '{name}' was found"
    return finish(outcome or pacer.outcome() or sc.STALLED, note, images)
