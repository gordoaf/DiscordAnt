"""The scraper's memory: one local SQLite file that keeps

  - every message ever captured (by channel),
  - which stretches of each channel's history are *fully* covered, so a later
    job only scrolls through what is genuinely missing,
  - channel / server names and user names seen along the way,
  - the pictures seen and where each one was saved.

Message ids are Discord snowflakes: they sort by time, so a stretch of history
is simply an inclusive id range [lo, hi].
"""

import json
import os
import sqlite3
import tempfile
import threading
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages(
    channel_id TEXT NOT NULL,
    id         INTEGER NOT NULL,
    ts         TEXT,
    author_id  TEXT,
    rec        TEXT NOT NULL,
    PRIMARY KEY(channel_id, id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS messages_author ON messages(author_id, id);
CREATE TABLE IF NOT EXISTS coverage(
    channel_id TEXT NOT NULL,
    lo         INTEGER NOT NULL,
    hi         INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS coverage_channel ON coverage(channel_id, lo);
CREATE TABLE IF NOT EXISTS names(
    id       TEXT PRIMARY KEY,
    kind     TEXT NOT NULL,
    name     TEXT NOT NULL,
    guild_id TEXT
);
CREATE TABLE IF NOT EXISTS users(
    id          TEXT PRIMARY KEY,
    username    TEXT,
    global_name TEXT
);
CREATE TABLE IF NOT EXISTS images(
    key        TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    kind       TEXT,
    name       TEXT,
    match      TEXT,
    url        TEXT,
    file       TEXT,
    w          INTEGER,
    h          INTEGER,
    bytes      INTEGER DEFAULT 0,
    full       INTEGER DEFAULT 0,
    generation INTEGER NOT NULL DEFAULT 1,
    identity   TEXT
);
CREATE INDEX IF NOT EXISTS images_message ON images(channel_id, message_id);
CREATE INDEX IF NOT EXISTS images_match ON images(match);
CREATE TABLE IF NOT EXISTS image_versions(
    key TEXT PRIMARY KEY,
    generation INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS context_done(
    channel_id TEXT NOT NULL,
    id         INTEGER NOT NULL,
    PRIMARY KEY(channel_id, id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS context_proofs(
    channel_id TEXT NOT NULL,
    id INTEGER NOT NULL,
    before_count INTEGER NOT NULL,
    after_count INTEGER NOT NULL,
    at_start INTEGER NOT NULL,
    at_end INTEGER NOT NULL,
    PRIMARY KEY(channel_id, id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS archive_meta(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS context_refresh(
    channel_id TEXT NOT NULL,
    id INTEGER NOT NULL,
    PRIMARY KEY(channel_id, id)
) WITHOUT ROWID;
"""

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
IMAGE_COLS = ("key", "channel_id", "message_id", "kind", "name", "match", "url",
              "file", "w", "h", "bytes", "full", "generation", "identity")


def is_image_attachment(att):
    ctype = (att.get("ctype") or "").lower()
    if ctype:
        return ctype.startswith("image/")
    return (att.get("name") or "").lower().endswith(IMAGE_EXTS)


def url_path(url):
    try:
        return urlparse(url or "").path
    except ValueError:
        return ""


def image_identity(url):
    """Identify the resource, ignoring only known Discord URL decorations."""
    try:
        parsed = urlparse(url or "")
        query = parsed.query
        if (parsed.hostname or "").lower() in (
                "cdn.discordapp.com", "media.discordapp.net", "images-ext-1.discordapp.net",
                "images-ext-2.discordapp.net"):
            ignored = {"ex", "is", "hm", "width", "height", "format", "quality"}
            query = urlencode([(k, v) for k, v in parse_qsl(query, keep_blank_values=True)
                               if k.lower() not in ignored])
        return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path,
                           parsed.params, query, ""))
    except ValueError:
        return str(url or "")


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False,
                                  isolation_level=None, timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        try:
            self._migrate()
        except Exception:
            self.db.close()
            raise

    def _meta(self, key):
        row = self.db.execute("SELECT value FROM archive_meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def _set_meta(self, key, value):
        self.db.execute("INSERT INTO archive_meta(key,value) VALUES(?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def _has_table(self, name):
        return self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                               (name,)).fetchone() is not None

    def _migrate(self):
        """Bring an older archive up to date in one all-or-nothing step.

        Messages and picture files are never removed, and neither is the
        record of which stretches of a channel were read in full: that record
        is made the same way in every version, so an archive from 1.0.0 keeps
        it. (1.0.1 emptied it on upgrade; it is put back here.)

        Picture references are rebuilt once so that each belongs to exactly
        one message. Older "conversation loaded" marks (context_done) are kept
        but no longer relied on. Records are streamed, not loaded into memory.
        """
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                columns = {r[1] for r in self.db.execute("PRAGMA table_info(images)")}
                if "generation" not in columns:
                    self.db.execute("ALTER TABLE images ADD COLUMN generation INTEGER NOT NULL DEFAULT 1")
                if "identity" not in columns:
                    self.db.execute("ALTER TABLE images ADD COLUMN identity TEXT")

                proof = self._meta("proof_version")
                if proof != "3":
                    if proof == "2" and self._has_table("coverage_legacy"):
                        # An archive that 1.0.1 upgraded: return what it set
                        # aside, merged with anything read since. Old search
                        # records ("user:...") stay aside; searches use new keys.
                        legacy = self.db.execute(
                            "SELECT channel_id,lo,hi FROM coverage_legacy "
                            "WHERE channel_id NOT LIKE 'user:%'").fetchall()
                        for channel_id, lo, hi in legacy:
                            if lo is not None and hi is not None and int(hi) >= int(lo):
                                self._merge_coverage(channel_id, int(lo), int(hi))
                    self._set_meta("proof_version", "3")

                images = self._meta("image_version")
                if images != "3":
                    if images is None and self.db.execute(
                            "SELECT 1 FROM images LIMIT 1").fetchone():
                        # From 1.0.0: one reference per picture -> one per message.
                        self.db.execute(
                            "CREATE TABLE IF NOT EXISTS images_legacy AS SELECT * FROM images")
                        self.db.execute("DELETE FROM images")
                        cursor = self.db.execute("SELECT channel_id,rec FROM messages")
                        while True:
                            batch = cursor.fetchmany(200)
                            if not batch:
                                break
                            for channel_id, raw in batch:
                                self._index_images(channel_id, json.loads(raw), migrating=True)
                    elif images == "2" and self._has_table("images_legacy"):
                        # From 1.0.1, which dropped every saved embed picture
                        # from the listing: relist the ones that are still right.
                        rows = self.db.execute(
                            "SELECT key,channel_id,message_id,match FROM images "
                            "WHERE kind='embed' AND file IS NULL").fetchall()
                        for key, channel_id, mid, match in rows:
                            rec = self.get(channel_id, mid) or {}
                            self._restore_legacy_embed(key, channel_id, mid,
                                                       key.rsplit(":", 1)[-1], match,
                                                       bool(rec.get("edited")))
                    self._set_meta("image_version", "3")
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def _restore_legacy_embed(self, key, channel_id, mid, slot, match, edited):
        """Relist a picture saved by 1.0.0 for an embed, when it is still the
        right one: same message, same position, same link, the message was
        never edited (an edit can swap what a position shows) and the file is
        still there. Anything else is left to be fetched again."""
        if edited:
            return
        saved = self.db.execute(
            "SELECT file,bytes,full FROM images_legacy WHERE key=? AND channel_id=? "
            "AND kind='embed' AND match IS ? AND file IS NOT NULL",
            (f"e:{int(mid)}:{slot}", channel_id, match)).fetchone()
        if saved and Path(saved[0]).is_file():
            self.db.execute("UPDATE images SET file=?,bytes=?,full=? WHERE key=?",
                            (*saved, key))

    def close(self):
        with self.lock:
            try:
                self.db.close()
            except Exception:
                pass

    # ------------------------------------------------------------ messages
    def put_messages(self, channel_id, recs, replace=True):
        """Store normalized message records. Returns the ids that were new.

        replace=False keeps an existing copy (used for search hits, which carry
        less detail than the same message fetched from its channel).
        """
        new_ids = []
        if not recs:
            return new_ids
        with self.lock:
            self.db.execute("BEGIN")
            try:
                for rec in recs:
                    mid = int(rec["id"])
                    had = self.db.execute(
                        "SELECT rec FROM messages WHERE channel_id=? AND id=?",
                        (channel_id, mid)).fetchone()
                    # A record marked "imported" came from a plain export file
                    # and is the thinnest kind (no author id), so anything
                    # may replace it.
                    if had and not replace and '"imported":true' not in had[0]:
                        continue
                    if had and rec.get("imported"):
                        continue        # never downgrade to an imported copy
                    self.db.execute(
                        "INSERT OR REPLACE INTO messages(channel_id,id,ts,author_id,rec) "
                        "VALUES(?,?,?,?,?)",
                        (channel_id, mid, rec.get("ts"), rec.get("author_id"),
                         json.dumps(rec, ensure_ascii=False, separators=(",", ":"))))
                    if not had:
                        new_ids.append(mid)
                    self._index_images(channel_id, rec)
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        return new_ids

    def get(self, channel_id, message_id):
        with self.lock:
            row = self.db.execute(
                "SELECT rec FROM messages WHERE channel_id=? AND id=?",
                (channel_id, int(message_id))).fetchone()
        return json.loads(row[0]) if row else None

    def messages_in(self, channel_id, lo, hi):
        with self.lock:
            rows = self.db.execute(
                "SELECT rec FROM messages WHERE channel_id=? AND id BETWEEN ? AND ? "
                "ORDER BY id", (channel_id, int(lo), int(hi))).fetchall()
        return [json.loads(r[0]) for r in rows]

    def count_in(self, channel_id, lo, hi):
        with self.lock:
            return self.db.execute(
                "SELECT COUNT(*) FROM messages WHERE channel_id=? AND id BETWEEN ? AND ?",
                (channel_id, int(lo), int(hi))).fetchone()[0]

    def neighbours(self, channel_id, message_id, k):
        """(k messages before, k messages after) the given id, both oldest first."""
        mid = int(message_id)
        with self.lock:
            before = self.db.execute(
                "SELECT rec FROM messages WHERE channel_id=? AND id<? "
                "ORDER BY id DESC LIMIT ?", (channel_id, mid, k)).fetchall()
            after = self.db.execute(
                "SELECT rec FROM messages WHERE channel_id=? AND id>? "
                "ORDER BY id LIMIT ?", (channel_id, mid, k)).fetchall()
        return ([json.loads(r[0]) for r in reversed(before)],
                [json.loads(r[0]) for r in after])

    def by_author(self, author_id, lo, hi, channel_ids=None):
        """[(channel_id, rec)] for one author inside an id range, oldest first."""
        with self.lock:
            rows = self.db.execute(
                "SELECT channel_id, rec FROM messages WHERE author_id=? "
                "AND id BETWEEN ? AND ? ORDER BY id",
                (str(author_id), int(lo), int(hi))).fetchall()
        out = [(c, json.loads(r)) for c, r in rows]
        if channel_ids is not None:
            keep = set(channel_ids)
            out = [x for x in out if x[0] in keep]
        return out

    def stats(self):
        with self.lock:
            n, ch = self.db.execute(
                "SELECT COUNT(*), COUNT(DISTINCT channel_id) FROM messages").fetchone()
            im = self.db.execute(
                "SELECT COUNT(*), SUM(file IS NOT NULL) FROM images").fetchone()
        return {"messages": n, "channels": ch, "images": im[0] or 0,
                "images_saved": im[1] or 0}

    # ------------------------------------------------------------ coverage
    def add_coverage(self, channel_id, lo, hi):
        """Record that every message with lo <= id <= hi is in the store."""
        lo, hi = int(lo), int(hi)
        if hi < lo:
            return
        with self.lock:
            self.db.execute("BEGIN")
            try:
                self._merge_coverage(channel_id, lo, hi)
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def _merge_coverage(self, channel_id, lo, hi):
        """Add [lo, hi], joining it with stretches it touches. The caller holds
        the lock and an open transaction."""
        rows = self.db.execute(
            "SELECT rowid, lo, hi FROM coverage WHERE channel_id=? "
            "AND lo<=? AND hi>=?", (channel_id, hi + 1, lo - 1)).fetchall()
        for rowid, l, h in rows:
            lo, hi = min(lo, l), max(hi, h)
            self.db.execute("DELETE FROM coverage WHERE rowid=?", (rowid,))
        self.db.execute("INSERT INTO coverage(channel_id,lo,hi) VALUES(?,?,?)",
                        (channel_id, lo, hi))

    def coverage(self, channel_id):
        with self.lock:
            return self.db.execute(
                "SELECT lo, hi FROM coverage WHERE channel_id=? ORDER BY lo",
                (channel_id,)).fetchall()

    def interval_containing(self, channel_id, message_id):
        mid = int(message_id)
        with self.lock:
            return self.db.execute(
                "SELECT lo, hi FROM coverage WHERE channel_id=? AND lo<=? AND hi>=?",
                (channel_id, mid, mid)).fetchone()

    def covered_below(self, channel_id, message_id):
        """Where the newest covered stretch that ends before message_id ends,
        or None when nothing older is covered."""
        with self.lock:
            row = self.db.execute(
                "SELECT MAX(hi) FROM coverage WHERE channel_id=? AND hi<?",
                (channel_id, int(message_id))).fetchone()
        return row[0] if row else None

    def gaps(self, channel_id, lo, hi):
        """Stretches of [lo, hi] NOT yet covered, newest first."""
        lo, hi = int(lo), int(hi)
        out, cursor = [], lo
        for l, h in self.coverage(channel_id):
            if h < cursor:
                continue
            if l > hi:
                break
            if l > cursor:
                out.append((cursor, min(l - 1, hi)))
            cursor = max(cursor, h + 1)
            if cursor > hi:
                break
        if cursor <= hi:
            out.append((cursor, hi))
        out.reverse()
        return out

    def clear_coverage(self, channel_id, lo, hi):
        """Forget that [lo, hi] is covered (the messages themselves are kept)."""
        lo, hi = int(lo), int(hi)
        if hi < lo:
            return
        with self.lock:
            rows = self.db.execute(
                "SELECT rowid, lo, hi FROM coverage WHERE channel_id=? "
                "AND lo<=? AND hi>=?", (channel_id, hi, lo)).fetchall()
            self.db.execute("BEGIN")
            try:
                for rowid, l, h in rows:
                    self.db.execute("DELETE FROM coverage WHERE rowid=?", (rowid,))
                    if l < lo:
                        self.db.execute(
                            "INSERT INTO coverage(channel_id,lo,hi) VALUES(?,?,?)",
                            (channel_id, l, lo - 1))
                    if h > hi:
                        self.db.execute(
                            "INSERT INTO coverage(channel_id,lo,hi) VALUES(?,?,?)",
                            (channel_id, hi + 1, h))
                # A proof may rely on neighbours outside its target ID. A fresh
                # channel range invalidates all its context proofs conservatively.
                self.db.execute("DELETE FROM context_proofs WHERE channel_id=?", (channel_id,))
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def has_context(self, channel_id, message_id, k):
        """True when k stored messages sit on each side of the message inside
        one covered stretch (so the conversation around it is really there)."""
        mid, k = int(message_id), max(0, int(k))
        if self.get(channel_id, mid) is None:
            return False
        iv = self.interval_containing(channel_id, mid)
        if not iv:
            return False
        lo, hi = iv
        with self.lock:
            below = self.db.execute(
                "SELECT COUNT(*) FROM (SELECT 1 FROM messages WHERE channel_id=? "
                "AND id<? AND id>=? LIMIT ?)", (channel_id, mid, lo, k)).fetchone()[0]
            above = self.db.execute(
                "SELECT COUNT(*) FROM (SELECT 1 FROM messages WHERE channel_id=? "
                "AND id>? AND id<=? LIMIT ?)", (channel_id, mid, hi, k)).fetchone()[0]
        return (below >= k or lo == 0) and above >= k

    # ------------------------------------------------------------ names
    def set_name(self, id_, kind, name, guild_id=None):
        name = (name or "").strip()
        if not id_ or not name:
            return
        with self.lock:
            self.db.execute(
                "INSERT INTO names(id,kind,name,guild_id) VALUES(?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name, kind=excluded.kind, "
                "guild_id=COALESCE(excluded.guild_id, names.guild_id)",
                (str(id_), kind, name, guild_id))

    def set_guild(self, channel_id, guild_id):
        """Remember which server a channel belongs to (name may come later)."""
        if not channel_id or not guild_id:
            return
        with self.lock:
            self.db.execute(
                "INSERT INTO names(id,kind,name,guild_id) VALUES(?,'channel','',?) "
                "ON CONFLICT(id) DO UPDATE SET guild_id=excluded.guild_id",
                (str(channel_id), str(guild_id)))

    def name(self, id_):
        with self.lock:
            row = self.db.execute("SELECT name FROM names WHERE id=?",
                                  (str(id_),)).fetchone()
        return (row[0] or None) if row else None

    def all_channels(self):
        """[(channel_id, name, guild_id)] for every channel with a known name."""
        with self.lock:
            return self.db.execute(
                "SELECT id, name, guild_id FROM names WHERE kind='channel' "
                "AND name<>''").fetchall()

    # ------------------------------------------------------------ context marks
    def mark_context(self, channel_id, message_id, k=0, before=0, after=0,
                     at_start=False, at_end=False):
        """Save a target-correlated proof supplied by the channel fetcher.

        Counts describe the messages in that verified response, not unrelated
        archive neighbours. Boundary flags require authenticated endpoint proof.
        Legacy unqualified context_done marks are never consulted.
        """
        mid, k, before, after = int(message_id), int(k), int(before), int(after)
        if min(k, before, after) < 0:
            raise ValueError("Context counts must be nonnegative")
        if not (before >= k or at_start) or not (after >= k or at_end):
            return False
        with self.lock:
            if self.get(channel_id, mid) is None:
                return False
            below, above = self.neighbours(channel_id, mid, max(before, after))
            if len(below) < before or len(above) < after:
                return False
            self.db.execute(
                "INSERT INTO context_proofs(channel_id,id,before_count,after_count,at_start,at_end) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(channel_id,id) DO UPDATE SET "
                "before_count=MAX(before_count,excluded.before_count), "
                "after_count=MAX(after_count,excluded.after_count), "
                "at_start=MAX(at_start,excluded.at_start), at_end=MAX(at_end,excluded.at_end)",
                (channel_id, mid, before, after, int(bool(at_start)), int(bool(at_end))))
            self.db.execute("DELETE FROM context_refresh WHERE channel_id=? AND id=?",
                            (channel_id, mid))
            return True

    def context_marked(self, channel_id, message_id, k=0):
        k = max(0, int(k))
        with self.lock:
            row = self.db.execute(
                "SELECT before_count,after_count,at_start,at_end FROM context_proofs "
                "WHERE channel_id=? AND id=?", (channel_id, int(message_id))).fetchone()
        return bool(row and (row[0] >= k or row[2]) and (row[1] >= k or row[3]))

    def clear_context(self, channel_id, lo, hi):
        """Invalidate verified context targets in a fresh user-job range."""
        with self.lock:
            self.db.execute("DELETE FROM context_proofs WHERE channel_id=? AND id BETWEEN ? AND ?",
                            (channel_id, int(lo), int(hi)))

    def request_context_refresh(self, channel_id, message_id):
        """Persist Start over's full-message refresh intent across interruptions."""
        with self.lock:
            self.db.execute("SAVEPOINT context_refresh_request")
            try:
                self.db.execute("DELETE FROM context_proofs WHERE channel_id=? AND id=?",
                                (channel_id, int(message_id)))
                self.db.execute("INSERT OR IGNORE INTO context_refresh(channel_id,id) VALUES(?,?)",
                                (channel_id, int(message_id)))
                self.db.execute("RELEASE context_refresh_request")
            except Exception:
                self.db.execute("ROLLBACK TO context_refresh_request")
                self.db.execute("RELEASE context_refresh_request")
                raise

    def context_refresh_pending(self, channel_id, message_id):
        with self.lock:
            return self.db.execute("SELECT 1 FROM context_refresh WHERE channel_id=? AND id=?",
                                   (channel_id, int(message_id))).fetchone() is not None

    def clear_author_context(self, guild_id, author_id, lo, hi):
        """Forget proofs for one author's range without deleting archive data."""
        with self.lock:
            self.db.execute(
                "DELETE FROM context_proofs WHERE id BETWEEN ? AND ? AND "
                "EXISTS(SELECT 1 FROM messages m JOIN names n ON n.id=m.channel_id "
                "WHERE m.channel_id=context_proofs.channel_id AND m.id=context_proofs.id "
                "AND m.author_id=? AND n.guild_id=?)",
                (int(lo), int(hi), str(author_id), str(guild_id)))

    def guild_of(self, channel_id):
        with self.lock:
            row = self.db.execute("SELECT guild_id FROM names WHERE id=?",
                                  (str(channel_id),)).fetchone()
        return row[0] if row else None

    def guilds(self):
        with self.lock:
            return self.db.execute(
                "SELECT id, name FROM names WHERE kind='guild' ORDER BY name").fetchall()

    def guild_ids(self):
        """Every server id the archive has heard of (named or not)."""
        with self.lock:
            rows = self.db.execute(
                "SELECT id FROM names WHERE kind='guild' UNION "
                "SELECT guild_id FROM names WHERE guild_id IS NOT NULL").fetchall()
        return [r[0] for r in rows if r[0]]

    def main_channel(self, guild_id):
        """The channel of a server holding the most archived messages, or None."""
        with self.lock:
            row = self.db.execute(
                "SELECT m.channel_id FROM messages m JOIN names n ON n.id=m.channel_id "
                "WHERE n.guild_id=? GROUP BY m.channel_id ORDER BY COUNT(*) DESC LIMIT 1",
                (str(guild_id),)).fetchone()
        return row[0] if row else None

    def channels_of(self, guild_id):
        with self.lock:
            return self.db.execute(
                "SELECT id, name FROM names WHERE kind='channel' AND guild_id=? "
                "AND name<>'' ORDER BY name", (str(guild_id),)).fetchall()

    def upsert_user(self, id_, username, global_name=None):
        if not id_:
            return
        with self.lock:
            self.db.execute(
                "INSERT INTO users(id,username,global_name) VALUES(?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "username=COALESCE(excluded.username, users.username), "
                "global_name=COALESCE(excluded.global_name, users.global_name)",
                (str(id_), username, global_name))

    def user(self, id_):
        with self.lock:
            row = self.db.execute(
                "SELECT id, username, global_name FROM users WHERE id=?",
                (str(id_),)).fetchone()
        return row

    def find_user(self, name):
        """User id for an exact username (or, failing that, display name)."""
        name = (name or "").strip().lstrip("@").lower()
        if not name:
            return None
        with self.lock:
            row = self.db.execute(
                "SELECT id FROM users WHERE LOWER(username)=?", (name,)).fetchone()
            if not row:
                rows = self.db.execute(
                    "SELECT id FROM users WHERE LOWER(global_name)=?", (name,)).fetchall()
                row = rows[0] if len(rows) == 1 else None
        return row[0] if row else None

    # ------------------------------------------------------------ images
    def _index_images(self, channel_id, rec, migrating=False):
        mid = int(rec["id"])
        keep = []
        for att in rec.get("attachments") or []:
            if not isinstance(att, dict) or not att.get("id"):
                continue
            if not is_image_attachment(att):
                continue
            key = f"a:{channel_id}:{mid}:{att['id']}"
            keep.append(key)
            self._upsert_image(key, channel_id, mid, "attachment",
                               att.get("name"), str(att["id"]),
                               att.get("proxy") or att.get("url"),
                               att.get("w"), att.get("h"), "attachment:" + str(att["id"]))
            if migrating:
                # An attachment id always means the same file, so a picture
                # saved for it stays valid for every message that carries it.
                saved = self.db.execute(
                    "SELECT file,bytes,full FROM images_legacy WHERE kind='attachment' "
                    "AND match=? AND file IS NOT NULL ORDER BY full DESC,bytes DESC LIMIT 1",
                    (str(att["id"]),)).fetchone()
                if saved:
                    self.db.execute("UPDATE images SET file=?,bytes=?,full=? WHERE key=?",
                                    (*saved, key))
        for i, emb in enumerate(rec.get("embeds") or []):
            img = emb.get("img") if isinstance(emb, dict) else None
            if not img:
                continue
            link = img.get("proxy") or img.get("url")
            if not link:
                continue
            name = url_path(img.get("url") or link).rsplit("/", 1)[-1] or "embed"
            key = f"e:{channel_id}:{mid}:{i}"
            keep.append(key)
            self._upsert_image(key, channel_id, mid, "embed", name[:80],
                               url_path(link), link, img.get("w"), img.get("h"),
                               image_identity(img.get("url") or link))
            if migrating:
                self._restore_legacy_embed(key, channel_id, mid, i, url_path(link),
                                           bool(rec.get("edited")))
        # Reconcile current references only; historical files are left intact.
        rows = self.db.execute("SELECT key FROM images WHERE channel_id=? AND message_id=?",
                               (channel_id, mid)).fetchall()
        wanted = set(keep)
        for (key,) in rows:
            if key not in wanted:
                self.db.execute("DELETE FROM images WHERE key=?", (key,))

    def _upsert_image(self, key, channel_id, mid, kind, name, match, url, w, h, identity):
        current = self.db.execute("SELECT identity,generation FROM images WHERE key=?", (key,)).fetchone()
        if current and current[0] == identity:
            generation = current[1]
        else:
            generation = self.db.execute(
                "INSERT INTO image_versions(key,generation) VALUES(?,1) "
                "ON CONFLICT(key) DO UPDATE SET generation=generation+1 RETURNING generation",
                (key,)).fetchone()[0]
        self.db.execute(
            "INSERT INTO images(key,channel_id,message_id,kind,name,match,url,w,h,identity,generation) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET url=excluded.url, match=excluded.match, "
            "name=excluded.name, w=excluded.w, h=excluded.h, "
            "file=CASE WHEN images.identity IS excluded.identity THEN images.file ELSE NULL END, "
            "bytes=CASE WHEN images.identity IS excluded.identity THEN images.bytes ELSE 0 END, "
            "full=CASE WHEN images.identity IS excluded.identity THEN images.full ELSE 0 END, "
            "generation=excluded.generation, identity=excluded.identity",
            (key, channel_id, mid, kind, name, match, url, w, h, identity, generation))

    def _image_rows(self, where, args):
        with self.lock:
            rows = self.db.execute(
                f"SELECT {','.join(IMAGE_COLS)} FROM images WHERE {where}", args).fetchall()
        return [dict(zip(IMAGE_COLS, r)) for r in rows]

    def image(self, key):
        rows = self._image_rows("key=?", (key,))
        return rows[0] if rows else None

    def image_by_match(self, match):
        rows = self._image_rows("match=? ORDER BY message_id DESC LIMIT 1", (match,))
        return rows[0] if rows else None

    def images_by_match(self, match):
        return self._image_rows("match=? ORDER BY message_id DESC", (match,))

    def images_by_attachment(self, attachment_id):
        return self._image_rows("kind='attachment' AND match=? ORDER BY message_id DESC",
                                (str(attachment_id),))

    def images_for(self, channel_id, message_id):
        return self._image_rows("channel_id=? AND message_id=? ORDER BY key",
                                (channel_id, int(message_id)))

    def images_in(self, channel_id, lo, hi):
        return self._image_rows(
            "channel_id=? AND message_id BETWEEN ? AND ? ORDER BY message_id DESC",
            (channel_id, int(lo), int(hi)))

    def set_image_file(self, key, file, nbytes, full, expected_generation=None):
        """Register a synthetic/imported file, optionally guarding its generation.

        Browser callbacks use save_image_file so checks and disk writes are one
        critical section. This helper also refuses quality downgrades.
        """
        with self.lock:
            row = self.image(key)
            if not self._accept_image(row, expected_generation, int(nbytes), full):
                return False
            self.db.execute("UPDATE images SET file=?, bytes=?, full=? WHERE key=?",
                            (str(file), int(nbytes), 1 if full else 0, key))
            return True

    @staticmethod
    def _accept_image(current, generation, nbytes, full):
        if not current or (generation is not None and current["generation"] != generation):
            return False
        if current["full"] and not full and current.get("file") and Path(current["file"]).is_file():
            return False
        if current.get("file") and bool(current["full"]) == bool(full):
            if nbytes <= (current["bytes"] or 0) and Path(current["file"]).is_file():
                return False
        return True

    def save_image_file(self, row, file, body, full):
        """Atomically publish a still-current image response without downgrades.

        A late callback cannot overwrite a replacement resource or a completed
        full image. Files already referenced by legacy/other rows are preserved.
        The caller validates the generated destination against its image root.
        """
        path = Path(file)
        with self.lock:
            current = self.image(row["key"])
            if not self._accept_image(current, row.get("generation"), len(body), full):
                return False
            # Require the caller's version token, including for old callbacks.
            if row.get("generation") is None:
                return False
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = None
            try:
                with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + ".",
                                                 suffix=".tmp", delete=False) as handle:
                    tmp = Path(handle.name)
                    handle.write(body)
                os.replace(tmp, path)
                self.db.execute("UPDATE images SET file=?,bytes=?,full=? WHERE key=? AND generation=?",
                                (str(path), len(body), int(bool(full)), row["key"], row["generation"]))
                return True
            finally:
                if tmp is not None:
                    try:
                        tmp.unlink(missing_ok=True)
                    except OSError:
                        pass
