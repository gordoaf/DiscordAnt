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
import sqlite3
import threading
from pathlib import Path
from urllib.parse import urlparse

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
    full       INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS images_message ON images(channel_id, message_id);
CREATE INDEX IF NOT EXISTS images_match ON images(match);
CREATE TABLE IF NOT EXISTS context_done(
    channel_id TEXT NOT NULL,
    id         INTEGER NOT NULL,
    PRIMARY KEY(channel_id, id)
) WITHOUT ROWID;
"""

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
IMAGE_COLS = ("key", "channel_id", "message_id", "kind", "name", "match", "url",
              "file", "w", "h", "bytes", "full")


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
            rows = self.db.execute(
                "SELECT rowid, lo, hi FROM coverage WHERE channel_id=? "
                "AND lo<=? AND hi>=?", (channel_id, hi + 1, lo - 1)).fetchall()
            for _rowid, l, h in rows:
                lo, hi = min(lo, l), max(hi, h)
            self.db.execute("BEGIN")
            try:
                for rowid, _l, _h in rows:
                    self.db.execute("DELETE FROM coverage WHERE rowid=?", (rowid,))
                self.db.execute("INSERT INTO coverage(channel_id,lo,hi) VALUES(?,?,?)",
                                (channel_id, lo, hi))
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise

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
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def has_context(self, channel_id, message_id, k):
        """True when k stored messages sit on each side of the message inside
        one covered stretch (so the conversation around it is really there)."""
        mid = int(message_id)
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
    def mark_context(self, channel_id, message_id):
        """Note that the conversation around this message has been fetched."""
        with self.lock:
            self.db.execute(
                "INSERT OR IGNORE INTO context_done(channel_id,id) VALUES(?,?)",
                (channel_id, int(message_id)))

    def context_marked(self, channel_id, message_id):
        with self.lock:
            return self.db.execute(
                "SELECT 1 FROM context_done WHERE channel_id=? AND id=?",
                (channel_id, int(message_id))).fetchone() is not None

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
    def _index_images(self, channel_id, rec):
        mid = int(rec["id"])
        for att in rec.get("attachments") or []:
            if not isinstance(att, dict) or not att.get("id"):
                continue
            if not is_image_attachment(att):
                continue
            self._upsert_image(f"a:{att['id']}", channel_id, mid, "attachment",
                               att.get("name"), str(att["id"]),
                               att.get("proxy") or att.get("url"),
                               att.get("w"), att.get("h"))
        for i, emb in enumerate(rec.get("embeds") or []):
            img = emb.get("img") if isinstance(emb, dict) else None
            if not img:
                continue
            link = img.get("proxy") or img.get("url")
            if not link:
                continue
            name = url_path(img.get("url") or link).rsplit("/", 1)[-1] or "embed"
            self._upsert_image(f"e:{mid}:{i}", channel_id, mid, "embed", name[:80],
                               url_path(link), link, img.get("w"), img.get("h"))

    def _upsert_image(self, key, channel_id, mid, kind, name, match, url, w, h):
        self.db.execute(
            "INSERT INTO images(key,channel_id,message_id,kind,name,match,url,w,h) "
            "VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET url=excluded.url, match=excluded.match, "
            "name=excluded.name, w=excluded.w, h=excluded.h",
            (key, channel_id, mid, kind, name, match, url, w, h))

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

    def images_for(self, channel_id, message_id):
        return self._image_rows("channel_id=? AND message_id=? ORDER BY key",
                                (channel_id, int(message_id)))

    def images_in(self, channel_id, lo, hi):
        return self._image_rows(
            "channel_id=? AND message_id BETWEEN ? AND ? ORDER BY message_id DESC",
            (channel_id, int(lo), int(hi)))

    def set_image_file(self, key, file, nbytes, full):
        with self.lock:
            self.db.execute("UPDATE images SET file=?, bytes=?, full=? WHERE key=?",
                            (str(file), int(nbytes), 1 if full else 0, key))
