"""Queue jobs and the naming rules around them. No Tk in here, so all of it
can be tested without opening a window."""

import re
from pathlib import Path

try:
    from . import scraper_core as sc
except ImportError:  # running as a plain script / frozen exe
    import scraper_core as sc

PENDING, RUNNING, DONE, FAILED, STOPPED = "pending", "running", "done", "failed", "stopped"
INCOMPLETE = "incomplete"   # ran, saved data, but did NOT cover the whole range
RESUMABLE = (PENDING, RUNNING, STOPPED, INCOMPLETE)
CHANNEL_JOB, USER_JOB = "channel", "user"

# A stand-in label made from the channel id ("channel 8435", "ch-8435"): it is
# swapped for the channel's real name as soon as the archive knows it.
PLACEHOLDER_RE = re.compile(r"^(channel[ _]|ch[-_])\d{4}$", re.I)
SERVER_SPLIT_RE = re.compile(r"\s+[|—–\-·•:]\s+|:\s+")
SERVER_MAX = 28


def is_placeholder(label):
    label = (label or "").strip()
    return not label or bool(PLACEHOLDER_RE.match(label))


def placeholder(channel_id):
    return f"channel {str(channel_id)[-4:]}"


def short_server(name):
    """'Gardening | Official Server' -> 'Gardening': a server name short
    enough to sit in front of a channel name."""
    name = (name or "").strip()
    if not name:
        return None
    first = SERVER_SPLIT_RE.split(name)[0]
    first = re.sub(r"^\W+", "", first, flags=re.UNICODE)[:SERVER_MAX].strip()
    return first or name[:SERVER_MAX].strip()


def real_label(store, guild_id, channel_id):
    """'<Server> <channel>' from the names the archive has learned, or None."""
    name = store.name(channel_id)
    if not name:
        return None
    pretty = sc.pretty_channel(name)
    server = short_server(store.name(guild_id)) if guild_id else None
    if not server or pretty.lower().startswith(server.lower()):
        return pretty
    return f"{server} {pretty}"


def guess_server(labels):
    """The first word shared by every real label ('Gardening general',
    'Gardening help' -> 'Gardening'), or None when they don't agree."""
    real = [l.split() for l in labels if not is_placeholder(l)]
    real = [w for w in real if w]
    if len(real) < 2:
        return None
    firsts = {w[0].lower() for w in real}
    return real[0][0] if len(firsts) == 1 else None


def file_name(job):
    span = f"{job.get('end') or 'start'}_to_{job.get('start') or 'now'}"
    if job.get("type") == USER_JOB:
        return (f"discord_user_{sc.safe_name(job.get('username'))}_"
                f"{sc.safe_name(job.get('server') or 'server')[:40]}_"
                f"g{job['guild_id']}_{span}.json")
    return (f"discord_{sc.safe_name(job.get('label'))[:60]}_"
            f"g{job['guild_id']}_c{job['channel_id']}_{span}.json")


def output_paths(job):
    out = Path(job["out"])
    return [out, out.with_suffix(".md")] if job.get("type") == USER_JOB else [out]


def new_job(kind, *, label, auto_label, guild_id, channel_id, start, end,
            out="", auto_out=True, username="", server=""):
    """A queue entry. Every key exists from the start: the worker thread only
    ever changes values, never the set of keys."""
    return {"type": kind, "label": label, "auto_label": bool(auto_label),
            "guild_id": str(guild_id), "channel_id": str(channel_id),
            "username": username, "server": server,
            "start": start, "end": end, "out": str(out), "auto_out": bool(auto_out),
            "status": PENDING, "note": "", "fresh": False,
            "output_hashes": {}, "batch_id": "", "batch_run": ""}


def fill_job(job):
    """Give a job loaded from an older queue file the keys it lacks. Returns
    False when the entry is not a usable job."""
    if not isinstance(job, dict):
        return False
    if not all(isinstance(job.get(k), str) and job[k]
               for k in ("guild_id", "channel_id", "out")):
        return False
    if not job["guild_id"].isdigit() or not job["channel_id"].isdigit():
        return False
    if "\x00" in job["out"]:
        return False
    job.setdefault("type", CHANNEL_JOB)
    if job["type"] not in (CHANNEL_JOB, USER_JOB):
        return False
    job.setdefault("username", "")
    job.setdefault("server", "")
    job.setdefault("label", placeholder(job["channel_id"]))
    if not all(isinstance(job.get(k), str) for k in ("username", "server", "label")):
        return False
    job.setdefault("auto_label", is_placeholder(job["label"]))
    for k in ("start", "end", "note"):
        if not isinstance(job.get(k), str):
            job[k] = ""
    job.setdefault("status", PENDING)
    if job["status"] not in (*RESUMABLE, DONE, FAILED):
        job["status"] = INCOMPLETE
    job.setdefault("fresh", False)
    if not all(isinstance(job.get(k, False), bool) for k in ("auto_label", "auto_out", "fresh")):
        return False
    if "auto_out" not in job:
        name = job["out"].replace("\\", "/").rsplit("/", 1)[-1]
        job["auto_out"] = name == file_name(job)
    if job["type"] == USER_JOB and not job["username"]:
        return False
    for key in ("batch_id", "batch_run"):
        if not isinstance(job.get(key), str):
            job[key] = ""
    hashes = job.get("output_hashes")
    if not isinstance(hashes, dict) or not all(
            isinstance(k, str) and (v is None or isinstance(v, str))
            for k, v in hashes.items()):
        job["output_hashes"] = {}
    return True


def outcome_status(job, res):
    """(status, note for the queue table) from what a finished job returned."""
    kind = job.get("type", CHANNEL_JOB)
    count = res.get("count", 0)
    outcome = res.get("outcome")
    if outcome == sc.STOPPED:
        return STOPPED, f"{count} messages so far"
    if (kind == USER_JOB and not res.get("author_id") and outcome != sc.BUDGET
            and not res.get("deferred_recent")):
        return FAILED, (res.get("note")
                        or f"no user called '{job.get('username')}' was found")
    if outcome in (sc.END_DATE, sc.TOP):
        if kind == USER_JOB:
            return DONE, f"{count} messages in {res.get('channels', 0)} channel(s)"
        note = f"{count} messages ({res.get('new', 0)} new)"
        return DONE, note + (" (whole channel)" if outcome == sc.TOP else "")
    if outcome == sc.BUDGET:
        why = "hit the time limit"
    else:
        why = res.get("note") or "history stopped loading"
    return INCOMPLETE, f"{count} messages — {why}"
