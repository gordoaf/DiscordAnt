import json
import random
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.store import Store, image_identity


def rec(mid, **extra):
    return dict(id=str(mid), ts="2026-11-11T00:00:00+00:00", author_id="99",
                author="test", content="synthetic", **extra)


def attachment(mid=100, aid="900"):
    return rec(mid, attachments=[{"id": aid, "name": "image.png", "ctype": "image/png",
                                  "url": "https://cdn.discordapp.com/attachments/1/900/image.png"}])


def embed(url, mid=100):
    return rec(mid, embeds=[{"img": {"url": url, "proxy": url}}])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "archive.db")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_attachment_removal_reconciles_current_refs_preserves_file(self):
        self.store.put_messages("1", [attachment()])
        row = self.store.images_for("1", 100)[0]
        saved = self.root / "saved.png"
        self.assertTrue(self.store.save_image_file(row, saved, b"old image", True))
        self.store.put_messages("1", [rec(100)])
        self.assertEqual(self.store.images_for("1", 100), [])
        self.assertEqual(saved.read_bytes(), b"old image")

    def test_changed_embed_resets_saved_state_and_rejects_old_callback(self):
        self.store.put_messages("1", [embed("https://example.invalid/old.png")])
        old = self.store.images_for("1", 100)[0]
        path = self.root / "old.png"
        self.store.save_image_file(old, path, b"old full image", True)
        self.store.put_messages("1", [embed("https://example.invalid/new.png")])
        new = self.store.images_for("1", 100)[0]
        self.assertGreater(new["generation"], old["generation"])
        self.assertEqual((new["file"], new["bytes"], new["full"]), (None, 0, 0))
        blocked = self.root / "blocked.png"
        self.assertFalse(self.store.save_image_file(old, blocked, b"late old image", True))
        self.assertFalse(blocked.exists())
        self.assertEqual(path.read_bytes(), b"old full image")

    def test_removed_then_readded_image_rejects_prior_generation(self):
        self.store.put_messages("1", [attachment()])
        old = self.store.images_for("1", 100)[0]
        self.store.put_messages("1", [rec(100)])
        self.store.put_messages("1", [attachment()])
        new = self.store.images_for("1", 100)[0]
        self.assertGreater(new["generation"], old["generation"])
        self.assertFalse(self.store.save_image_file(old, self.root / "bad.png", b"stale", True))

    def test_signature_refresh_preserves_image_arbitrary_query_change_does_not(self):
        base = "https://media.discordapp.net/external/hash/image.png"
        self.store.put_messages("1", [embed(base + "?ex=a&hm=old&width=10")])
        row = self.store.images_for("1", 100)[0]
        saved = self.root / "full.png"
        self.store.save_image_file(row, saved, b"full", True)
        self.store.put_messages("1", [embed(base + "?ex=b&hm=new&width=100")])
        refreshed = self.store.images_for("1", 100)[0]
        self.assertEqual(refreshed["generation"], row["generation"])
        self.assertEqual(refreshed["file"], str(saved))
        self.assertNotEqual(image_identity("https://example.invalid/image?id=1"),
                            image_identity("https://example.invalid/image?id=2"))

    def test_repeated_attachment_ids_have_independent_message_refs(self):
        self.store.put_messages("1", [attachment(100)])
        self.store.put_messages("2", [attachment(200)])
        rows = self.store.images_by_attachment("900")
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]["key"], rows[1]["key"])
        for row in rows:
            self.assertTrue(self.store.save_image_file(
                row, self.root / (row["channel_id"] + ".png"), b"shared picture", True))
        self.assertEqual(len(self.store.images_for("1", 100)), 1)
        self.assertEqual(len(self.store.images_for("2", 200)), 1)
        self.store.put_messages("1", [rec(100)])
        self.assertEqual(len(self.store.images_by_attachment("900")), 1)
        self.assertTrue(Path(self.store.images_for("2", 200)[0]["file"]).exists())

    def test_late_thumbnail_cannot_downgrade_full_image(self):
        self.store.put_messages("1", [attachment()])
        stale = self.store.images_for("1", 100)[0]
        path = self.root / "picture.png"
        self.assertTrue(self.store.save_image_file(stale, path, b"FULL", True))
        self.assertFalse(self.store.save_image_file(stale, path, b"a much larger thumbnail", False))
        self.assertEqual(path.read_bytes(), b"FULL")
        self.assertEqual(self.store.image(stale["key"])["full"], 1)

    def test_full_upgrade_allowed_even_when_smaller_than_thumbnail(self):
        self.store.put_messages("1", [attachment()])
        row = self.store.images_for("1", 100)[0]
        path = self.root / "picture.png"
        self.assertTrue(self.store.save_image_file(row, path, b"large preview", False))
        self.assertTrue(self.store.save_image_file(row, path, b"full", True))
        self.assertEqual(path.read_bytes(), b"full")

    def test_missing_saved_file_can_be_repaired(self):
        self.store.put_messages("1", [attachment()])
        row = self.store.images_for("1", 100)[0]
        self.store.set_image_file(row["key"], self.root / "missing.png", 1000, True)
        self.assertTrue(self.store.save_image_file(row, self.root / "repair.png", b"full", True))

    def test_thumbnail_can_repair_missing_full_file_without_claiming_full(self):
        self.store.put_messages("1", [attachment()])
        row = self.store.images_for("1", 100)[0]
        self.store.set_image_file(row["key"], self.root / "missing.png", 1000, True)
        path = self.root / "thumbnail.png"
        self.assertTrue(self.store.save_image_file(row, path, b"preview", False))
        repaired = self.store.image(row["key"])
        self.assertEqual(repaired["full"], 0)
        self.assertEqual(repaired["bytes"], 7)
        self.assertEqual(path.read_bytes(), b"preview")

    def test_context_proof_respects_counts_and_verified_boundaries(self):
        self.store.put_messages("1", [rec(i) for i in (10, 20, 30)])
        self.assertFalse(self.store.mark_context("1", 20, 2, before=1, after=1))
        self.assertTrue(self.store.mark_context("1", 20, 1, before=1, after=1))
        self.assertTrue(self.store.context_marked("1", 20, 1))
        self.assertFalse(self.store.context_marked("1", 20, 2))
        self.assertFalse(self.store.mark_context("1", 999, 1, at_start=True, at_end=True))
        self.assertTrue(self.store.mark_context("1", 10, 10, before=0, after=2,
                                                at_start=True, at_end=True))
        self.assertTrue(self.store.context_marked("1", 10, 100))

    def test_clear_context_and_coverage_invalidate_proofs(self):
        self.store.put_messages("1", [rec(i) for i in (10, 20, 30)])
        self.store.mark_context("1", 20, 1, before=1, after=1)
        self.store.clear_context("1", 20, 20)
        self.assertFalse(self.store.context_marked("1", 20, 1))
        self.store.mark_context("1", 20, 1, before=1, after=1)
        self.store.add_coverage("1", 0, 100)
        self.store.clear_coverage("1", 99, 100)
        self.assertFalse(self.store.context_marked("1", 20, 1))

    def test_fresh_author_context_only_invalidates_matching_guild_author(self):
        for channel, guild in (("1", "g1"), ("2", "g2")):
            self.store.set_guild(channel, guild)
            self.store.put_messages(channel, [rec(20)])
            self.store.mark_context(channel, 20, 1, at_start=True, at_end=True)
        self.store.clear_author_context("g1", "99", 0, 100)
        self.assertFalse(self.store.context_marked("1", 20, 1))
        self.assertTrue(self.store.context_marked("2", 20, 1))

    def test_refresh_intent_persists_until_successful_verified_proof(self):
        self.store.put_messages("1", [rec(20)])
        self.store.mark_context("1", 20, 1, at_start=True, at_end=True)
        self.store.request_context_refresh("1", 20)
        self.assertFalse(self.store.context_marked("1", 20, 1))
        self.store.close()
        self.store = Store(self.root / "archive.db")
        self.assertTrue(self.store.context_refresh_pending("1", 20))
        self.assertFalse(self.store.mark_context("1", 20, 1))
        self.assertTrue(self.store.context_refresh_pending("1", 20))
        self.assertTrue(self.store.mark_context("1", 20, 1, at_start=True, at_end=True))
        self.assertFalse(self.store.context_refresh_pending("1", 20))

    def test_message_transaction_rollback(self):
        with self.assertRaises(ValueError):
            self.store.put_messages("1", [attachment(), rec("bad")])
        self.assertEqual(self.store.count_in("1", 0, 1000), 0)
        self.assertEqual(self.store.images_by_attachment("900"), [])

    def test_coverage_matches_independent_set_model(self):
        rng, covered = random.Random(12), set()
        for _ in range(100):
            lo, hi = sorted((rng.randrange(100), rng.randrange(100)))
            if rng.randrange(2):
                self.store.add_coverage("1", lo, hi)
                covered.update(range(lo, hi + 1))
            else:
                self.store.clear_coverage("1", lo, hi)
                covered.difference_update(range(lo, hi + 1))
            actual = {i for a, b in self.store.gaps("1", 0, 99) for i in range(a, b + 1)}
            self.assertEqual(actual, set(range(100)) - covered)


class LegacyMigrationTests(unittest.TestCase):
    def test_migration_failure_rolls_back_current_data_and_proof_version(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "corrupt-record.db"
            old = sqlite3.connect(path)
            old.executescript("""
                CREATE TABLE messages(channel_id TEXT,id INTEGER,ts TEXT,author_id TEXT,rec TEXT,
                                      PRIMARY KEY(channel_id,id)) WITHOUT ROWID;
                CREATE TABLE coverage(channel_id TEXT,lo INTEGER,hi INTEGER);
                CREATE TABLE images(key TEXT PRIMARY KEY,channel_id TEXT,message_id INTEGER,
                    kind TEXT,name TEXT,match TEXT,url TEXT,file TEXT,w INTEGER,h INTEGER,
                    bytes INTEGER DEFAULT 0,full INTEGER DEFAULT 0);
                INSERT INTO messages VALUES('1',100,NULL,NULL,'invalid JSON');
                INSERT INTO coverage VALUES('1',0,500);
                INSERT INTO images(key,channel_id,message_id,kind)
                    VALUES('a:900','1',100,'attachment');
            """)
            old.commit()
            old.close()
            with self.assertRaises(json.JSONDecodeError):
                Store(path)
            reopened = sqlite3.connect(path)
            try:
                self.assertEqual(reopened.execute("SELECT * FROM coverage").fetchall(), [("1", 0, 500)])
                self.assertEqual(reopened.execute("SELECT rec FROM messages").fetchone()[0], "invalid JSON")
                self.assertEqual(reopened.execute("SELECT COUNT(*) FROM archive_meta").fetchone()[0], 0)
                self.assertNotIn("generation", {r[1] for r in reopened.execute("PRAGMA table_info(images)")})
            finally:
                reopened.close()

    def test_migration_preserves_records_files_and_legacy_evidence_once(self):
        with tempfile.TemporaryDirectory() as td:
            path, saved = Path(td) / "old.db", Path(td) / "old.png"
            saved.write_bytes(b"preserve this old image")
            old = sqlite3.connect(path)
            old.executescript("""
                CREATE TABLE messages(channel_id TEXT,id INTEGER,ts TEXT,author_id TEXT,rec TEXT,
                                      PRIMARY KEY(channel_id,id)) WITHOUT ROWID;
                CREATE TABLE coverage(channel_id TEXT,lo INTEGER,hi INTEGER);
                CREATE TABLE images(key TEXT PRIMARY KEY,channel_id TEXT,message_id INTEGER,
                    kind TEXT,name TEXT,match TEXT,url TEXT,file TEXT,w INTEGER,h INTEGER,
                    bytes INTEGER DEFAULT 0,full INTEGER DEFAULT 0);
                CREATE TABLE context_done(channel_id TEXT,id INTEGER,PRIMARY KEY(channel_id,id));
            """)
            for channel, mid in (("1", 100), ("2", 200)):
                message = attachment(mid)
                old.execute("INSERT INTO messages VALUES(?,?,?,?,?)",
                            (channel, mid, message["ts"], "99", json.dumps(message)))
            old.execute("INSERT INTO coverage VALUES('1',0,500)")
            old.execute("INSERT INTO coverage VALUES('user:1:99',0,500)")
            old.execute("INSERT INTO context_done VALUES('1',100)")
            old.execute("INSERT INTO images VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        ("a:900", "1", 100, "attachment", "image.png", "900", "oldurl",
                         str(saved), None, None, saved.stat().st_size, 1))
            old.commit()
            old.close()
            store = Store(path)
            try:
                self.assertEqual(store.count_in("1", 0, 500), 1)
                # What 1.0.0 recorded as fully read is still recorded.
                self.assertEqual(store.coverage("1"), [(0, 500)])
                self.assertEqual(store.gaps("1", 0, 500), [])
                self.assertFalse(store._has_table("coverage_legacy"))
                self.assertFalse(store.context_marked("1", 100, 1))
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM context_done").fetchone()[0], 1)
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM images_legacy").fetchone()[0], 1)
                self.assertEqual(len(store.images_by_attachment("900")), 2)
                self.assertTrue(all(r["file"] == str(saved) for r in store.images_by_attachment("900")))
                store.add_coverage("1", 400, 600)
            finally:
                store.close()
            store = Store(path)
            try:
                self.assertEqual(store.coverage("1"), [(0, 600)])
                self.assertEqual(saved.read_bytes(), b"preserve this old image")
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM images_legacy").fetchone()[0], 1)
            finally:
                store.close()

    def test_new_archive_needs_no_legacy_tables(self):
        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "new.db")
            try:
                self.assertEqual(store._meta("proof_version"), "3")
                self.assertEqual(store._meta("image_version"), "3")
                self.assertFalse(store._has_table("coverage_legacy"))
                self.assertFalse(store._has_table("images_legacy"))
            finally:
                store.close()

    def as_left_by_1_0_1(self, path, legacy, current):
        """An archive in the state 1.0.1 put it in: the record of what was
        read set aside in coverage_legacy, plus whatever was read afterwards."""
        store = Store(path)
        store.put_messages("1", [rec(100)])
        store.db.execute("CREATE TABLE coverage_legacy AS SELECT * FROM coverage")
        store.db.executemany("INSERT INTO coverage_legacy(channel_id,lo,hi) VALUES(?,?,?)", legacy)
        store.db.execute("DELETE FROM coverage")
        store.db.executemany("INSERT INTO coverage(channel_id,lo,hi) VALUES(?,?,?)", current)
        store.db.execute("UPDATE archive_meta SET value='2'")
        store.close()

    def test_archive_upgraded_by_1_0_1_gets_its_record_back_once(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "was-101.db"
            self.as_left_by_1_0_1(
                path,
                legacy=[("1", 0, 500), ("1", 900, 950), ("2", 10, 20), ("user:1:99", 0, 500)],
                current=[("1", 450, 700), ("user:v2:1:99", 5, 6)])
            store = Store(path)
            try:
                self.assertEqual(store.coverage("1"), [(0, 700), (900, 950)])
                self.assertEqual(store.coverage("2"), [(10, 20)])
                self.assertEqual(store.coverage("user:1:99"), [])       # old search records stay aside
                self.assertEqual(store.coverage("user:v2:1:99"), [(5, 6)])
                self.assertEqual(store._meta("proof_version"), "3")
                store.clear_coverage("1", 0, 100)                       # a later "Start over"
            finally:
                store.close()
            store = Store(path)
            try:
                self.assertEqual(store.coverage("1"), [(101, 700), (900, 950)])   # not put back twice
            finally:
                store.close()

    def old_archive_with_embed(self, path, saved, edited=False, legacy_match=None):
        link = "https://images-ext-1.discordapp.net/external/SIG/https/example.com/pic.png"
        message = embed(link, mid=100)
        if edited:
            message["edited"] = True
        old = sqlite3.connect(path)
        old.executescript("""
            CREATE TABLE messages(channel_id TEXT,id INTEGER,ts TEXT,author_id TEXT,rec TEXT,
                                  PRIMARY KEY(channel_id,id)) WITHOUT ROWID;
            CREATE TABLE coverage(channel_id TEXT,lo INTEGER,hi INTEGER);
            CREATE TABLE images(key TEXT PRIMARY KEY,channel_id TEXT,message_id INTEGER,
                kind TEXT,name TEXT,match TEXT,url TEXT,file TEXT,w INTEGER,h INTEGER,
                bytes INTEGER DEFAULT 0,full INTEGER DEFAULT 0);
        """)
        old.execute("INSERT INTO messages VALUES(?,?,?,?,?)",
                    ("1", 100, message["ts"], "99", json.dumps(message)))
        old.execute("INSERT INTO images VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("e:100:0", "1", 100, "embed", "pic.png",
                     legacy_match or "/external/SIG/https/example.com/pic.png", link,
                     str(saved), None, None, 7, 1))
        old.commit()
        old.close()

    def test_saved_embed_picture_stays_listed_when_it_is_still_the_right_one(self):
        with tempfile.TemporaryDirectory() as td:
            saved = Path(td) / "pic.png"
            saved.write_bytes(b"picture")
            cases = (("kept", {}, str(saved)),
                     ("edited message", {"edited": True}, None),
                     ("different link", {"legacy_match": "/external/OTHER/pic.png"}, None))
            for label, options, expected in cases:
                with self.subTest(label):
                    path = Path(td) / (label.replace(" ", "-") + ".db")
                    self.old_archive_with_embed(path, saved, **options)
                    store = Store(path)
                    try:
                        row = store.images_for("1", 100)[0]
                        self.assertEqual(row["file"], expected)
                        self.assertEqual(row["full"], 1 if expected else 0)
                    finally:
                        store.close()
            # the file gone from disk: not listed
            path = Path(td) / "missing.db"
            self.old_archive_with_embed(path, Path(td) / "nowhere.png")
            store = Store(path)
            try:
                self.assertIsNone(store.images_for("1", 100)[0]["file"])
            finally:
                store.close()
            self.assertEqual(saved.read_bytes(), b"picture")

    def test_embed_picture_unlisted_by_1_0_1_is_listed_again(self):
        with tempfile.TemporaryDirectory() as td:
            saved = Path(td) / "pic.png"
            saved.write_bytes(b"picture")
            path = Path(td) / "was-101.db"
            self.old_archive_with_embed(path, saved)
            store = Store(path)
            # put it in the state 1.0.1 left: listing dropped, versions at 2
            store.db.execute("UPDATE images SET file=NULL,bytes=0,full=0")
            store.db.execute("UPDATE archive_meta SET value='2'")
            store.close()
            store = Store(path)
            try:
                self.assertEqual(store.images_for("1", 100)[0]["file"], str(saved))
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
