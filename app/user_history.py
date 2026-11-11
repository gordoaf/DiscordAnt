"""User history: everything one person posted in a server (inside a date
range), together with the conversation around each message.

How it works, all inside the same real browser window at human pace:

  1. SEARCH — types `from:<user>` (plus dates) into Discord's own search bar
     and pages through the results with the Next button, reading the answers
     Discord's client receives. That finds every message of theirs in every
     channel of the server the account can see.
  2. CONVERSATIONS — for each message whose surroundings are not in the
     archive yet, jumps to it (as clicking "Jump" on a search result does), so
     Discord loads the messages around it — including the full message being
     replied to.
  3. Writes a JSON file grouped by channel, and a readable .md transcript.

Both steps remember their progress in the archive, so a stopped job carries on
where it left off and nothing is looked up twice.
"""

import asyncio
import json
import os
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from . import scraper_core as sc
except ImportError:  # running as a plain script / frozen exe
    import scraper_core as sc

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

JS_OPTIONS = """() => [...document.querySelectorAll('[role="listbox"] [role="option"]')]
    .map(o => ({sel: o.getAttribute('aria-selected'),
                text: (o.innerText || '').trim().slice(0, 80),
                user: (o.querySelector('[role="img"][aria-label]') || {getAttribute() { return null; }})
                      .getAttribute('aria-label')}))"""

JS_NEXT_READY = """() => {
    const b = document.querySelector('button[rel="next"]');
    return !!b && !b.disabled && b.getAttribute('aria-disabled') !== 'true';
}"""

JS_JUMP = """(p) => {
    history.pushState({}, '', p);
    window.dispatchEvent(new PopStateEvent('popstate', {state: {}}));
}"""


class SearchProblem(Exception):
    """The search could not be set up (shown to the user as the job's note)."""


def user_key(guild_id, author_id):
    """Archive key under which 'this stretch has been searched' is recorded."""
    return f"user:{guild_id}:{author_id}"


def search_now():
    return sc.dt_to_snowflake(datetime.now(timezone.utc)
                              - timedelta(seconds=SEARCH_NOW_MARGIN_S))


# ------------------------------------------------------------------ search
async def _type(page, text):
    await page.keyboard.type(text, delay=random.randint(TYPE_MIN_MS, TYPE_MAX_MS))


async def _await_search(ses, n0, timeout=None):
    """Wait for a usable search answer that arrived after index n0."""
    t_end = time.monotonic() + (SEARCH_WAIT_S if timeout is None else timeout)
    seen = n0
    while time.monotonic() < t_end:
        if ses.pacer.stopped:
            return None
        while len(ses.searches) > seen:
            entry = ses.searches[seen]
            seen += 1
            if entry["ok"]:
                return entry
            # "still indexing" / "slow down": Discord's client retries by itself
            wait = float(entry.get("retry_after") or 5)
            t_end = max(t_end, time.monotonic() + wait + 25)
        await asyncio.sleep(0.25)
    return None


async def _start_search(ses, name, lo, hi, log):
    """Type the query for one stretch [lo, hi] and run it. Returns the first
    page's answer."""
    page, pacer = ses.page, ses.pacer
    await page.bring_to_front()
    await page.keyboard.press("Control+f")
    await pacer.nap(1.0, 1.6)
    await page.keyboard.press("Control+a")
    await pacer.nap(0.3, 0.6)
    await page.keyboard.press("Backspace")
    await pacer.nap(0.8, 1.2)

    if lo > 0:
        day = (sc.snowflake_to_dt(lo) - timedelta(days=DATE_PAD_DAYS)).date()
        await _type(page, "after:")
        await pacer.nap(0.5, 0.9)
        await _type(page, f"{day.isoformat()} ")
        await pacer.nap(0.8, 1.2)
    top = sc.snowflake_to_dt(hi) + timedelta(days=DATE_PAD_DAYS)
    if top < datetime.now(timezone.utc):
        await _type(page, "before:")
        await pacer.nap(0.5, 0.9)
        await _type(page, f"{top.date().isoformat()} ")
        await pacer.nap(0.8, 1.2)
    await _type(page, "from:")
    await pacer.nap(0.5, 0.9)
    await _type(page, name)
    await pacer.nap(1.8, 2.6)
    if pacer.stopped:
        return None

    # Pick the suggestion that is exactly this user.
    options = await page.evaluate(JS_OPTIONS)
    want = name.lower()
    idx = next((i for i, o in enumerate(options)
                if (o.get("user") or "").lower() == want), None)
    if idx is None:
        idx = next((i for i, o in enumerate(options) if o.get("user")
                    and (o.get("text") or "").split("\n")[0].strip().lower() == want), None)
    if idx is None:
        seen = [o["user"] for o in options if o.get("user")]
        await page.keyboard.press("Escape")
        hint = ("Discord suggested: " + ", ".join(seen[:8])) if seen else \
               "Discord suggested nobody for that name."
        raise SearchProblem(f"No user called '{name}' was found in this server. {hint} "
                            f"(Use the exact username, not the display name.)")
    cur = next((i for i, o in enumerate(options) if o.get("sel") == "true"), 0)
    for _ in range(abs(idx - cur)):
        await page.keyboard.press("ArrowDown" if idx > cur else "ArrowUp")
        await pacer.nap(0.2, 0.4)

    n0 = len(ses.searches)
    await page.keyboard.press("Enter")
    entry = await _await_search(ses, n0, timeout=ENTER_WAIT_S)
    if entry is None and not pacer.stopped and len(ses.searches) == n0:
        await page.keyboard.press("Enter")      # first Enter only picked the user
        entry = await _await_search(ses, n0)
    elif entry is None and not pacer.stopped:
        entry = await _await_search(ses, n0)
    if entry is None:
        if pacer.stopped:
            return None
        raise SearchProblem("Discord's search did not answer. Try again later.")
    q = entry["query"]
    if "author_id" not in q or "channel_id" in q:
        await page.keyboard.press("Escape")
        raise SearchProblem("The search did not come out as a server-wide 'from user' "
                            "search — Discord's search box may have changed. Nothing "
                            "was collected.")
    return entry


async def _search_pass(ses, store, guild_id, name, lo, hi, log):
    """Search one stretch and page through it, newest to oldest.

    Returns {"author_id", "hits", "pages", "cut_short", "problem"}.
    """
    page, pacer = ses.page, ses.pacer
    out = {"author_id": None, "hits": 0, "pages": 0, "cut_short": False,
           "problem": None}
    entry = await _start_search(ses, name, lo, hi, log)
    if entry is None:
        return out
    q = entry["query"]
    author_id = out["author_id"] = q["author_id"]
    try:
        qmin = int(q.get("min_id") or 0)
        qmax = int(q["max_id"]) if q.get("max_id") else None
    except ValueError:
        raise SearchProblem("Could not read the search's date range.")
    cov_hi = min(qmax, search_now()) if qmax is not None else search_now()

    oldest = None
    reached_end = False
    deep = False
    offset = 0
    while True:
        deep = deep or entry["deep"]
        total = entry["total"] or 0
        hits = entry["hits"]
        out["hits"] += len(hits)
        out["pages"] += 1
        wrong = [h for h in hits if h.get("author_id") and h["author_id"] != author_id]
        if wrong:
            raise SearchProblem("The search returned other people's messages — "
                                "stopped so nothing wrong is saved.")
        if hits:
            low = min(int(h["id"]) for h in hits)
            oldest = low if oldest is None else min(oldest, low)
        if offset == 0:
            log(f"Search found {total} message(s) in this stretch"
                + (" (Discord is still indexing old messages, so this may be "
                   "incomplete)" if deep else "") + ".")
        if not hits or offset + SEARCH_PAGE >= total:
            reached_end = True
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
            if not await page.evaluate(JS_NEXT_READY):
                break
            n0 = len(ses.searches)
            try:
                await page.click('button[rel="next"]', timeout=5000)
            except Exception:
                break
            nxt = await _await_search(ses, n0)
            if nxt is not None or pacer.stopped:
                break
            await pacer.nap(10, 20)
        if nxt is None:
            # No further page came. What was read so far is kept; the caller
            # searches again from there (Discord also caps how deep one search
            # can be paged, so a very long history takes several searches).
            out["cut_short"] = not pacer.stopped
            break
        try:
            offset = int(nxt["query"].get("offset") or 0)
        except ValueError:
            offset += SEARCH_PAGE
        entry = nxt

    # Record which stretch is now known to be fully searched.
    if deep:
        out["problem"] = "Discord is still indexing this server's old messages for search"
    else:
        cov_lo = qmin if reached_end else oldest
        if cov_lo is not None and cov_hi >= cov_lo:
            store.add_coverage(user_key(guild_id, author_id), cov_lo, cov_hi)
    return out


# ------------------------------------------------------------------ context
def user_hits(store, guild_id, author_id, lo, hi):
    """[(channel_id, rec)] — this user's archived messages in this server."""
    return [(ch, rec) for ch, rec in store.by_author(author_id, lo, hi)
            if store.guild_of(ch) == str(guild_id)]


def needs_context(store, ch, mid, k):
    return not (store.has_context(ch, mid, k) or store.context_marked(ch, mid))


async def _wait_fetch(ses, ch, n0, timeout):
    t_end = time.monotonic() + timeout
    while time.monotonic() < t_end:
        if any(f["channel"] == ch for f in ses.fetches[n0:]):
            return True
        await asyncio.sleep(0.25)
    return False


async def _load_context(ses, guild_id, ch, mid):
    """Jump to one message so Discord loads the conversation around it."""
    path = f"/channels/{guild_id}/{ch}/{mid}"
    n0 = len(ses.fetches)
    try:
        await ses.page.evaluate(JS_JUMP, path)
    except Exception:
        pass
    if await _wait_fetch(ses, ch, n0, JUMP_WAIT_S):
        return True
    # Discord already had this spot loaded (or ignored the jump): open the
    # message's link directly instead.
    n0 = len(ses.fetches)
    await ses.page.goto("https://discord.com" + path)
    return await _wait_fetch(ses, ch, n0, GOTO_WAIT_S)


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
    return (text or "").replace("\r", "").replace("\n", "\n        ")


def _md_line(row):
    mark = "▶" if row.get("theirs") else " "
    head = f"{mark} {row['ts'][11:16]} {row['author']}"
    if row.get("reply_to"):
        head += f" (↩ {row['reply_to']})"
    parts = [f"{head}: {_md_escape(row.get('content'))}".rstrip()]
    if row.get("forwarded"):
        parts.append(f"        [forwarded] {_md_escape(row['forwarded'])}")
    for e in row.get("embeds") or []:
        parts.append(f"        [link/embed] {_md_escape(e)}")
    shown = {im.get("name") for im in row.get("images") or []}
    for im in row.get("images") or []:
        parts.append(f"        [image {im.get('name') or ''}] {im.get('file') or ''}".rstrip())
    others = [a for a in row.get("attachments") or [] if a not in shown]
    if others:
        parts.append("        [files] " + ", ".join(others))
    if row.get("poll"):
        parts.append(f"        [poll] {row['poll']}")
    if row.get("stickers"):
        parts.append("        [sticker] " + ", ".join(row["stickers"]))
    if row.get("reactions"):
        parts.append(f"        [reactions] {row['reactions']}")
    return "\n".join(parts)


def export_user(store, guild_id, author_id, lo, hi, out_path, log,
                k=CONTEXT_K, note=None, start_dt=None, end_dt=None):
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
    doc["channels"] = channels
    out_path = Path(out_path)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, out_path)

    md = [f"# {username}" + (f" ({display})" if display != username else "")
          + f" in {server}",
          f"{doc['from']} to {doc['to']} · {len(hits)} messages in "
          f"{len(channels)} channel(s)", ""]
    if note:
        md += [f"**Note:** {note}", ""]
    md += [f"Lines marked ▶ are {username}'s. Unmarked lines are the conversation "
           f"around them. Times are UTC.", ""]
    for c in channels:
        md += [f"## #{c['channel']} — {c['messages']} message(s)", ""]
        for conv in c["conversations"]:
            day = None
            md.append("```")
            for row in conv:
                if row["ts"][:10] != day:
                    day = row["ts"][:10]
                    md.append(f"--- {day} ---")
                md.append(_md_line(row))
            md += ["```", ""]
    md_path = out_path.with_suffix(".md")
    md_path.write_text("\n".join(md), encoding="utf-8")

    log(f"Saved {len(hits)} messages by {username} across {len(channels)} channel(s).")
    log(f"Output: {out_path}")
    log(f"Readable transcript: {md_path}")
    return len(hits), len(channels)


# ------------------------------------------------------------------ one job
async def run_user_history(*, guild_id, channel_id, username, start_dt, end_dt,
                           out_path, profile_dir, log, stop_event, store,
                           max_minutes=None, pause_event=None, fresh=False,
                           images_dir=None, full_images=True, context_k=CONTEXT_K):
    """Run one user-history job. `channel_id` is any channel of the server
    (just the place the search is typed from).

    Returns a dict: count, channels, outcome (sc.END_DATE = complete,
    sc.STOPPED, sc.BUDGET or sc.STALLED), username, author_id (None when the
    user was never found), guild_name, images, note.
    """
    name = (username or "").strip().lstrip("@")
    if not name:
        raise ValueError("Enter a username.")
    win_lo, win_hi = sc.window_ids(start_dt, end_dt)
    if win_hi < win_lo:
        raise ValueError("That date range is in the future.")
    search_hi = min(win_hi, search_now())
    store.set_guild(channel_id, guild_id)
    # Only trust the archive's idea of who this is when the username matches
    # exactly; otherwise Discord's own search decides.
    author_id = store.find_user(name)
    known = store.user(author_id) if author_id else None
    if not known or (known[1] or "").lower() != name.lower():
        author_id = None
    if fresh and author_id:
        store.clear_coverage(user_key(guild_id, author_id), win_lo, win_hi)
        log("Starting over: this range will be searched again.")

    def finish(outcome, note=None, images=0):
        if not author_id:
            return {"count": 0, "channels": 0, "outcome": outcome, "username": name,
                    "author_id": None, "guild_name": store.name(guild_id),
                    "images": images, "note": note}
        count, nch = export_user(store, guild_id, author_id, win_lo, win_hi, out_path,
                                 log, k=context_k, note=note, start_dt=start_dt,
                                 end_dt=end_dt)
        urow = store.user(author_id)
        return {"count": count, "channels": nch, "outcome": outcome,
                "username": (urow[1] if urow else None) or name,
                "author_id": author_id, "guild_name": store.name(guild_id),
                "images": images, "note": note}

    def search_gaps():
        if not author_id or search_hi < win_lo:
            return [] if author_id else [(win_lo, search_hi)]
        return store.gaps(user_key(guild_id, author_id), win_lo, search_hi)

    def context_todo():
        if not author_id or context_k <= 0:
            return []
        todo = [(ch, int(rec["id"])) for ch, rec in
                user_hits(store, guild_id, author_id, win_lo, win_hi)
                if needs_context(store, ch, int(rec["id"]), context_k)]
        todo.sort(key=lambda x: (x[0], -x[1]))      # channel by channel, newest first
        return todo

    if not search_gaps() and not context_todo():
        log("Everything for this user and range is already in the archive.")
        return finish(sc.END_DATE)

    pacer = sc.Pacer(stop_event, pause_event, log, max_minutes)
    outcome = None
    note = None
    images = 0
    async with sc.Session(profile_dir=profile_dir, store=store, log=log, pacer=pacer,
                          images_dir=images_dir) as ses:
        if not await ses.open_app():
            return finish(sc.STOPPED)
        await ses.page.goto(f"https://discord.com/channels/{guild_id}/{channel_id}")
        await pacer.nap(6, 10)
        await ses.note_names(guild_id)

        # ---- 1. search
        passes = 0
        short_passes = 0
        while not (pacer.stopped or pacer.out_of_time()):
            gaps = search_gaps()
            if not gaps:
                break
            passes += 1
            if passes > MAX_SEARCH_PASSES:
                note = "the search needed too many passes; some messages may be missing"
                log(f"WARNING: {note}.")
                outcome = sc.STALLED
                break
            lo, hi = gaps[0]
            log(f"Searching {store.name(guild_id) or 'the server'} for messages from "
                f"{name} ({sc.snowflake_to_dt(lo):%Y-%m-%d} to "
                f"{sc.snowflake_to_dt(hi):%Y-%m-%d})..." if lo else
                f"Searching {store.name(guild_id) or 'the server'} for messages from "
                f"{name} (up to {sc.snowflake_to_dt(hi):%Y-%m-%d})...")
            try:
                res = await _search_pass(ses, store, guild_id, name, lo, hi, log)
            except SearchProblem as e:
                log(f"ERROR: {e}")
                note, outcome = str(e), sc.STALLED
                break
            if res["author_id"]:
                if author_id and res["author_id"] != author_id:
                    note = (f"'{name}' now matches a different account than before — "
                            f"stopped. Use the exact username.")
                    log("ERROR: " + note)
                    outcome = sc.STALLED
                    break
                author_id = res["author_id"]
            if res["problem"]:
                note, outcome = res["problem"], sc.STALLED
                log(f"WARNING: {note}.")
                break
            if pacer.stopped or not author_id:
                break
            # A pass that ended early (or moved nothing forward) gets a couple
            # more tries, then the job is marked incomplete.
            short = res["cut_short"] and res["pages"] < 200
            if short or search_gaps() == gaps:
                short_passes += 1
                if short_passes > MAX_SHORT_PASSES or search_gaps() == gaps:
                    note = "the search results stopped loading part-way"
                    log(f"WARNING: {note}.")
                    outcome = sc.STALLED
                    break
                await pacer.nap(15, 30)
            else:
                short_passes = 0
            await pacer.nap(2, 4)
        try:
            await ses.page.keyboard.press("Escape")
        except Exception:
            pass

        # ---- 2. the conversation around each message (also done for whatever
        #         an incomplete search did find)
        if author_id and not (pacer.stopped or pacer.out_of_time()):
            todo = context_todo()
            if todo:
                total = len(user_hits(store, guild_id, author_id, win_lo, win_hi))
                log(f"{total} message(s) found. Loading the conversation around "
                    f"them ({len(todo)} still to look up at most)...")
            fails = 0
            done = 0
            seen_channels = set()
            for ch, mid in todo:
                if pacer.stopped or pacer.out_of_time():
                    break
                if not needs_context(store, ch, mid, context_k):
                    continue            # an earlier look-up already covered it
                ok = await _load_context(ses, guild_id, ch, mid)
                del ses.fetches[:]
                if ok:
                    store.mark_context(ch, mid)
                    fails = 0
                    done += 1
                else:
                    fails += 1
                    if fails >= MAX_JUMP_FAILS:
                        note = note or "conversations stopped loading part-way"
                        log("WARNING: conversations stopped loading part-way.")
                        outcome = sc.STALLED
                        break
                if ch not in seen_channels:
                    seen_channels.add(ch)
                    await pacer.nap(1.0, 2.0)
                    await ses.note_names(guild_id)
                await pacer.step(lambda: f"{done} conversation(s) loaded",
                                 lo=JUMP_MIN_S, hi=JUMP_MAX_S)
            if done:
                log(f"Loaded {done} conversation(s).")

        if outcome is None:
            if pacer.stopped:
                outcome = sc.STOPPED
                log("Stopped by user. Progress is kept — run the job again to carry on.")
            elif pacer.out_of_time():
                outcome = sc.BUDGET
                log("Time budget reached.")
            elif search_gaps() or context_todo():
                outcome = sc.STALLED
            else:
                outcome = sc.END_DATE

        # ---- 3. full-size copies of the user's own images
        if (author_id and images_dir is not None and full_images
                and outcome in (sc.END_DATE, sc.STALLED)):
            rows = []
            for ch, rec in user_hits(store, guild_id, author_id, win_lo, win_hi):
                rows += store.images_for(ch, rec["id"])
            await ses.fetch_full_images(rows)
        images = ses.images_saved
        if images:
            log(f"Images saved this run: {images}")

    if not author_id and note is None and outcome not in (sc.STOPPED, sc.BUDGET):
        note = f"no user called '{name}' was found"
    return finish(outcome, note, images)
