import json
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from app import jobs, scraper_core as sc, user_history as uh
from app.store import Store


def record(mid=500, content="old"):
    return dict(id=str(mid), ts="2026-11-01T12:00:00+00:00", author_id="30",
                username="target", author="Target", content=content)


def raw_message(mid=500, content="new"):
    return dict(id=str(mid), channel_id="20", timestamp="2026-11-01T12:00:00+00:00",
                author=dict(id="30", username="target", global_name="Target"),
                content=content, hit=True)


def other_message(mid):
    return dict(id=str(mid), channel_id="20", timestamp="2026-11-01T12:00:00+00:00",
                author=dict(id="40", username="someone", global_name="Someone"), content="around")


def search_entry(ids=(), *, total=None, offset=0, sort="desc", **query):
    q = dict(author_id="30", min_id="100", max_id="1001", sort_by="timestamp",
             sort_order=sort, offset=str(offset))
    q.update(query)
    return dict(guild="10", query=q, ok=True, status=200, total=len(ids) if total is None else total,
                deep=False, hits=[dict(id=str(i), author_id="30", channel="20") for i in ids])


class FakePacer:
    def __init__(self, *args, **kwargs):
        self.stopped = False
        self.expired = False

    def out_of_time(self):
        return self.expired

    def outcome(self):
        return sc.STOPPED if self.stopped else sc.BUDGET if self.expired else None

    async def checkpoint(self):
        return self.outcome() is None

    async def nap(self, *args, **kwargs):
        return await self.checkpoint()

    async def step(self, *args, **kwargs):
        return await self.checkpoint()

    async def run(self, awaitable, **kwargs):
        if self.outcome():
            awaitable.close()
            raise sc.PacerInterrupted(self.outcome())
        return await awaitable


class UserHistoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.store = Store(self.folder / "archive.db")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def search(self, first, following=(), stop_after_first=False):
        pacer = FakePacer()
        ses = types.SimpleNamespace(pacer=pacer, searches=[])
        remaining = iter(following)
        class Page:
            async def evaluate(self, *_):
                return True
            async def click(self, *args, **kwargs):
                try:
                    entry = dict(next(remaining), request_started=time.monotonic())
                    ses.searches.append(entry)
                except StopIteration:
                    pacer.stopped = True
        ses.page = Page()
        async def start(*args):
            pacer.stopped = stop_after_first
            return first
        with patch.object(uh, "_start_search", start), patch.object(uh, "search_now", lambda: 1000):
            return await uh._search_pass(ses, self.store, "10", "target", 101, 1000, lambda *_: None)

    async def test_ascending_search_cannot_claim_coverage(self):
        with self.assertRaises(uh.SearchProblem):
            await self.search(search_entry(range(101, 126), total=100, sort="asc"), stop_after_first=True)
        self.assertEqual([], self.store.coverage(uh.user_key("10", "30")))

    async def test_unknown_sort_cannot_claim_coverage(self):
        entry = search_entry([200])
        del entry["query"]["sort_order"]
        with self.assertRaises(uh.SearchProblem):
            await self.search(entry)
        self.assertEqual([], self.store.coverage(uh.user_key("10", "30")))

    async def test_interrupted_descending_page_records_only_proven_suffix(self):
        result = await self.search(search_entry(range(200, 175, -1), total=100), stop_after_first=True)
        self.assertEqual(1, result["pages"])
        self.assertEqual([(176, 1000)], self.store.coverage(uh.user_key("10", "30")))
        self.assertEqual([(101, 175)], self.store.gaps(uh.user_key("10", "30"), 101, 1000))

    async def test_repeated_offset_does_not_mark_any_coverage(self):
        with self.assertRaises(uh.SearchProblem):
            await self.search(search_entry(range(200, 175, -1), total=26),
                              [search_entry([175], total=26, offset=0)])
        self.assertEqual([], self.store.coverage(uh.user_key("10", "30")))

    async def test_changed_filter_or_nonzero_first_offset_rejected(self):
        for entry in [search_entry([200], offset=25), search_entry([200], channel_id="20"),
                      search_entry([200], has="link"), dict(search_entry([200]), guild="11")]:
            with self.subTest(entry=entry), self.assertRaises(uh.SearchProblem):
                await self.search(entry)
        with self.assertRaises(uh.SearchProblem):
            await self.search(search_entry(range(200, 175, -1), total=26),
                              [search_entry([175], total=26, offset=25, max_id="1002")])
        self.assertEqual([], self.store.coverage(uh.user_key("10", "30")))

    async def test_descending_claim_requires_descending_disjoint_ids(self):
        for ids in [[198, 200, 199], [200, 200]]:
            with self.subTest(ids=ids), self.assertRaises(uh.SearchProblem):
                await self.search(search_entry(ids))
        self.assertEqual([], self.store.coverage(uh.user_key("10", "30")))

    async def test_results_that_move_while_paging_keep_the_pages_already_read(self):
        # The person posts while the search is being paged: the next page
        # starts with the message the last page ended on, and the count is up.
        key = uh.user_key("10", "30")
        for label, second in (("repeat", search_entry([176], total=26, offset=25)),
                              ("count", search_entry(range(175, 150, -1), total=61, offset=25))):
            with self.subTest(label):
                self.store.clear_coverage(key, 0, 2000)
                total = 26 if label == "repeat" else 60
                result = await self.search(search_entry(range(200, 175, -1), total=total), [second])
                self.assertTrue(result["cut_short"])
                self.assertEqual(1, result["pages"])
                # Page one stands; the moved page proves nothing below it.
                self.assertEqual([(176, 1000)], self.store.coverage(key))
                self.assertEqual([(101, 175)], self.store.gaps(key, 101, 1000))

    async def test_wrong_author_is_rejected_outright(self):
        wrong = search_entry([200])
        wrong["hits"][0]["author_id"] = "31"
        with self.assertRaises(uh.SearchProblem):
            await self.search(wrong)
        self.assertEqual([], self.store.coverage(uh.user_key("10", "30")))

    async def test_fewer_results_than_promised_keeps_only_what_was_read(self):
        key = uh.user_key("10", "30")
        result = await self.search(search_entry([], total=30))
        self.assertTrue(result["cut_short"])
        self.assertEqual([], self.store.coverage(key))
        result = await self.search(search_entry([200], total=30))
        self.assertTrue(result["cut_short"])
        self.assertEqual([(200, 1000)], self.store.coverage(key))
        # A later page that comes back short is treated the same way.
        self.store.clear_coverage(key, 0, 2000)
        result = await self.search(search_entry(range(200, 175, -1), total=60),
                                   [search_entry(range(175, 165, -1), total=60, offset=25)])
        self.assertTrue(result["cut_short"])
        self.assertEqual(2, result["pages"])
        self.assertEqual([(166, 1000)], self.store.coverage(key))

    async def test_first_search_ignores_delayed_same_guild_wrong_author(self):
        pacer = FakePacer()
        ses = types.SimpleNamespace(pacer=pacer, searches=[])
        current = search_entry([500])
        current["hits"][0]["username"] = "target"
        class Keyboard:
            async def type(self, *args, **kwargs):
                pass
            async def press(self, key):
                if key == "Enter":
                    wrong = search_entry([501], author_id="31")
                    wrong["hits"][0].update(author_id="31", username="someone_else")
                    wrong["request_started"] = time.monotonic() - 60
                    ses.searches.extend([wrong, dict(current, request_started=time.monotonic())])
        class Page:
            keyboard = Keyboard()
            async def bring_to_front(self):
                pass
            async def evaluate(self, *_):
                return [dict(sel="true", text="target", user="target", user_id=None)]
        ses.page = Page()
        entry = await uh._start_search(ses, "target", 101, 1000, lambda *_: None, "10")
        self.assertEqual("30", entry["query"]["author_id"])

    async def test_unknown_zero_hit_identity_and_old_requests_fail_closed(self):
        entry = dict(search_entry([]), request_started=20)
        self.assertFalse(uh._response_matches(entry, "10", 10, "target", lo=101, hi=1000))
        self.assertTrue(uh._response_matches(entry, "10", 10, "target", "30", 101, 1000))
        self.assertFalse(uh._response_matches(entry, "10", 30, "target", "30", 101, 1000))
        self.assertFalse(uh._response_matches(dict(entry, query=dict(entry["query"], max_id="999")),
                                             "10", 10, "target", "30", 101, 1000))

    async def test_valid_two_page_search_and_deep_index_safety(self):
        result = await self.search(search_entry(range(200, 175, -1), total=26),
                                   [search_entry([175], total=26, offset=25)])
        self.assertEqual(2, result["pages"])
        self.assertEqual([(101, 1000)], self.store.coverage(uh.user_key("10", "30")))
        self.store.clear_coverage(uh.user_key("10", "30"), 0, 1000)
        deep = dict(search_entry([200]), deep=True)
        result = await self.search(deep)
        self.assertIn("indexing", result["problem"])
        self.assertEqual([], self.store.coverage(uh.user_key("10", "30")))

    async def test_exclusive_query_bounds_do_not_overclaim(self):
        await self.search(search_entry([200], min_id="100", max_id="1000"))
        self.assertEqual([(101, 999)], self.store.coverage(uh.user_key("10", "30")))

    async def test_context_requires_target_and_correlated_request(self):
        unrelated = dict(channel="20", query={"around": "925"}, ids=tuple(range(900, 951)), at_start=False)
        self.assertIsNone(uh._context_proof(unrelated, "20", 500, 8))
        # The right request, but the message is no longer there (deleted): the
        # answer still counts, as what surrounds that spot now.
        gone = dict(channel="20", query={"around": "500"},
                    ids=tuple(i for i in range(485, 503) if i != 500), at_end=True)
        proof = uh._context_proof(gone, "20", 500, 8)
        self.assertTrue(proof["gone"])
        self.assertEqual((15, 2), (proof["before"], proof["after"]))
        self.assertTrue(proof["complete"])
        self.assertFalse(uh._context_proof(dict(gone, at_end=False), "20", 500, 8)["complete"])
        valid = dict(channel="20", query={"around": "500"}, ids=tuple(range(492, 509)))
        self.assertFalse(uh._context_proof(valid, "20", 500, 8)["gone"])
        self.assertTrue(uh._context_proof(valid, "20", 500, 8)["complete"])
        self.assertIsNone(uh._context_proof(dict(valid, request_started=10), "20", 500, 8, triggered=20))
        self.assertTrue(uh._context_proof(dict(valid, request_started=20), "20", 500, 8, triggered=20)["complete"])
        short = dict(valid, ids=(499, 500, 501))
        self.assertFalse(uh._context_proof(short, "20", 500, 8)["complete"])
        self.assertTrue(uh._context_proof(dict(short, at_start=True, at_end=True), "20", 500, 8)["complete"])

    async def test_legacy_context_mark_never_suppresses_retry(self):
        self.store.put_messages("20", [record()])
        self.store.db.execute("INSERT INTO context_done(channel_id,id) VALUES('20',500)")
        self.assertTrue(uh.needs_context(self.store, "20", 500, 8))
        self.assertEqual("user:v2:10:30", uh.user_key("10", "30"))

    async def test_search_and_context_waits_observe_budget(self):
        pacer = sc.Pacer(threading.Event(), max_minutes=0.0001)
        ses = types.SimpleNamespace(pacer=pacer, searches=[], fetches=[])
        start = time.monotonic()
        self.assertIsNone(await uh._await_search(ses, 0))
        self.assertIsNone(await uh._wait_fetch(ses, "20", 500, 0, 40))
        self.assertLess(time.monotonic() - start, 0.4)

    def seed_user(self):
        self.store.put_messages("20", [record()])
        self.store.upsert_user("30", "target", "Target")
        self.store.set_guild("20", "10")
        self.store.mark_context("20", 500, 0)
        self.store.add_coverage(uh.user_key("10", "30"), 101, 1000)

    def offline_session(self):
        class Keyboard:
            async def press(self, *_):
                pass
        class Page:
            keyboard = Keyboard()
            def __init__(self, ses):
                self.ses = ses
            async def goto(self, *_):
                pass
            async def evaluate(self, js, *_):
                self.ses._take_batch("20", "https://discord.com/api/v10/channels/20/messages?around=500&limit=50",
                                     [raw_message()], request_started=time.monotonic())
        class Offline(sc.Session):
            async def __aenter__(self):
                self.page = Page(self)
                return self
            async def __aexit__(self, *_):
                return False
            async def open_app(self):
                return True
            async def note_names(self, *_):
                pass
        return Offline

    async def invoke(self, **overrides):
        kwargs = dict(guild_id="10", channel_id="20", username="target", start_dt=None, end_dt=None,
                      out_path=self.folder / "out.json", profile_dir=self.folder / "forbidden",
                      log=lambda *_: None, stop_event=threading.Event(), store=self.store, context_k=0)
        kwargs.update(overrides)
        return await uh.run_user_history(**kwargs)

    async def test_start_over_refreshes_full_message_after_thin_search(self):
        self.seed_user()
        async def changed_search(ses, *_):
            ses._take_search("10", "https://discord.com/api/v10/guilds/10/messages/search?author_id=30&min_id=100&max_id=1001&sort_by=timestamp&sort_order=desc",
                             200, dict(total_results=1, messages=[[raw_message(content="thin search")]]))
            self.assertEqual("old", self.store.get("20", 500)["content"])
            return ses.searches[-1]
        with patch.object(sc, "Session", self.offline_session()), patch.object(sc, "Pacer", FakePacer), \
             patch.object(sc, "window_ids", lambda *_: (101, 1000)), \
             patch.object(uh, "search_now", lambda: 1000), patch.object(uh, "_start_search", changed_search):
            result = await self.invoke(fresh=True)
        self.assertEqual(sc.END_DATE, result["outcome"])
        self.assertEqual("new", self.store.get("20", 500)["content"])
        self.assertFalse(self.store.context_refresh_pending("20", 500))

    async def test_verified_zero_hit_user_keeps_requested_name(self):
        async def empty_search(*_):
            return search_entry([])
        with patch.object(sc, "Session", self.offline_session()), patch.object(sc, "Pacer", FakePacer), \
             patch.object(sc, "window_ids", lambda *_: (101, 1000)), \
             patch.object(uh, "search_now", lambda: 1000), patch.object(uh, "_start_search", empty_search):
            result = await self.invoke()
        self.assertEqual(sc.END_DATE, result["outcome"])
        self.assertEqual("target", result["username"])
        self.assertEqual(0, result["count"])

    async def test_interrupted_refresh_persists_for_ordinary_resume(self):
        self.seed_user()
        async def changed_search(*_):
            return search_entry([500])
        async def interrupted(*_):
            raise sc.PacerInterrupted(sc.BUDGET)
        patches = [patch.object(sc, "Session", self.offline_session()), patch.object(sc, "Pacer", FakePacer),
                   patch.object(sc, "window_ids", lambda *_: (101, 1000)), patch.object(uh, "search_now", lambda: 1000)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        with patch.object(uh, "_start_search", changed_search), patch.object(uh, "_load_context", interrupted):
            result = await self.invoke(fresh=True)
        self.assertEqual(sc.BUDGET, result["outcome"])
        self.assertTrue(self.store.context_refresh_pending("20", 500))
        result = await self.invoke()
        self.assertEqual(sc.END_DATE, result["outcome"])
        self.assertEqual("new", self.store.get("20", 500)["content"])
        self.assertFalse(self.store.context_refresh_pending("20", 500))

    async def test_job_up_to_now_finishes_and_says_where_it_stopped(self):
        # The last minutes are newer than Discord's search can see. Everything
        # older is collected, so the job is done; the files say how far it got.
        self.seed_user()
        self.store.clear_coverage(uh.user_key("10", "30"), 901, 1000)
        class NoBrowser:
            def __init__(self, **_):
                raise AssertionError("Covered eligible range must not open a browser")
        with patch.object(sc, "Session", NoBrowser), patch.object(sc, "window_ids", lambda *_: (101, 1000)), \
             patch.object(uh, "search_now", lambda: 900):
            result = await self.invoke()
        self.assertEqual(sc.END_DATE, result["outcome"])
        self.assertEqual(jobs.DONE, jobs.outcome_status({"type": jobs.USER_JOB}, result)[0])
        self.assertFalse(result["complete"])
        self.assertTrue(result["deferred_recent"])
        self.assertTrue(result["searched_any"])
        self.assertIn("Searched up to", result["note"])
        doc = json.loads((self.folder / "out.json").read_text(encoding="utf-8"))
        self.assertEqual(result["searched_through"], doc["searched_through"])
        self.assertFalse(doc["complete"])
        md = (self.folder / "out.md").read_text(encoding="utf-8")
        self.assertIn("Complete up to", md)
        self.assertNotIn("Complete: no", md)

    async def test_entire_recent_window_does_not_report_empty_complete(self):
        self.seed_user()
        with patch.object(sc, "window_ids", lambda *_: (901, 1000)), patch.object(uh, "search_now", lambda: 900):
            result = await self.invoke()
        self.assertEqual(sc.STALLED, result["outcome"])
        self.assertIsNone(result["searched_through"])
        self.assertFalse(result["searched_any"])
        self.assertEqual(jobs.INCOMPLETE, jobs.outcome_status({"type": jobs.USER_JOB}, result)[0])

    def edge_session(self, ids, limit=30):
        """A browser stand-in whose jump is answered with one 'around' page."""
        class Keyboard:
            async def press(self, *_):
                pass
        class Page:
            keyboard = Keyboard()
            def __init__(self, ses):
                self.ses = ses
            async def goto(self, *_):
                pass
            async def evaluate(self, js, *_):
                if js == uh.JS_JUMP:
                    self.ses._take_batch(
                        "20", f"https://discord.com/api/v10/channels/20/messages?limit={limit}&around=500",
                        [raw_message(500, "theirs") if i == 500 else other_message(i) for i in ids],
                        request_started=time.monotonic())
        class Offline(sc.Session):
            jumps = 0
            async def __aenter__(self):
                type(self).jumps += 1
                self.page = Page(self)
                return self
            async def __aexit__(self, *_):
                return False
            async def open_app(self):
                return True
            async def note_names(self, *_):
                pass
        return Offline

    async def run_edge(self, ids):
        self.store.put_messages("20", [record()])
        self.store.upsert_user("30", "target", "Target")
        self.store.set_guild("20", "10")
        self.store.add_coverage(uh.user_key("10", "30"), 101, 1000)
        session = self.edge_session(ids)
        outcomes = []
        with patch.object(sc, "Session", session), patch.object(sc, "Pacer", FakePacer), \
             patch.object(sc, "window_ids", lambda *_: (101, 1000)), \
             patch.object(uh, "search_now", lambda: 1000):
            for _ in range(2):
                outcomes.append((await self.invoke(context_k=8))["outcome"])
        return outcomes, session.jumps

    async def test_message_near_the_end_of_a_channel_finishes_and_is_not_revisited(self):
        outcomes, sessions = await self.run_edge(range(485, 503))          # two newer exist
        self.assertEqual([sc.END_DATE, sc.END_DATE], outcomes)
        self.assertEqual(1, sessions)        # the second run had nothing left to load

    async def test_message_near_the_start_of_a_channel_finishes(self):
        outcomes, sessions = await self.run_edge(range(498, 515))          # two older exist
        self.assertEqual([sc.END_DATE, sc.END_DATE], outcomes)
        self.assertEqual(1, sessions)

    async def test_deleted_message_does_not_keep_the_job_open(self):
        outcomes, sessions = await self.run_edge([i for i in range(485, 503) if i != 500])
        self.assertEqual([sc.END_DATE, sc.END_DATE], outcomes)
        self.assertEqual(1, sessions)

    async def test_message_in_the_middle_with_too_little_around_it_is_not_claimed(self):
        # A full page did not run out of channel, so five older messages are
        # simply too few: this one is (rightly) asked for again.
        outcomes, sessions = await self.run_edge(range(495, 525))          # 5 older, page full
        self.assertEqual([sc.STALLED, sc.STALLED], outcomes)
        self.assertEqual(2, sessions)

    async def test_markdown_names_and_content_cannot_break_structure(self):
        self.seed_user()
        self.store.upsert_user("30", "target", "<img src=x onerror=alert(1)>\n# forged")
        self.store.set_name("10", "guild", "<script>evil</script>\n# bad")
        self.store.set_name("20", "channel", "legit\n# fake", "10")
        self.store.put_messages("20", [dict(record(content="```\n# forged content\n```"), author="X\n```\n# forged author")])
        uh.export_user(self.store, "10", "30", 101, 1000, self.folder / "markdown.json", lambda *_: None, k=0,
                       note="<img src=x>\n# forged note")
        md = (self.folder / "markdown.md").read_text(encoding="utf-8")
        self.assertNotIn("<script>", md)
        self.assertNotIn("<img", md)
        self.assertNotIn("\n# forged author", md)
        self.assertNotIn("\n# forged note", md)
        self.assertIn("\n````\n", md)
        self.assertIn("&lt;script&gt;", md)


if __name__ == "__main__":
    unittest.main()
