import asyncio
import io
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from app import scraper_core as sc
from app.store import Store


CDN_URL = "https://cdn.discordapp.com/attachments/222/777/image.png"


def message(mid="444", **extra):
    return dict(id=mid, timestamp="2026-11-11T13:00:00Z",
                author={"id": "333", "username": "sample"}, content="synthetic", **extra)


def response(url, data=None, status=200):
    return types.SimpleNamespace(url=url, status=status,
                                 request=types.SimpleNamespace(method="GET"),
                                 headers={}, json=AsyncMock(return_value=data))


class URLTests(unittest.TestCase):
    def test_channel_link_requires_exact_https_origin_and_path(self):
        for good in ("https://discord.com/channels/111/222", "discord.com/channels/111/222",
                     "https://discord.com/channels/111/222/333?jump=1"):
            self.assertEqual(sc.parse_channel_link(good), ("111", "222"))
        for bad in ("https://evildiscord.com/channels/111/222", "http://discord.com/channels/111/222",
                    "https://discord.com.evil.invalid/channels/111/222",
                    "https://discord.com@evil.invalid/channels/111/222",
                    "https://user@discord.com/channels/111/222",
                    "https://discord.com:444/channels/111/222",
                    "https://example.invalid/?x=discord.com/channels/111/222",
                    "https://discord.com/prefix/channels/111/222",
                    "https://discord.com/channels/111/222suffix",
                    "https://discord.com/channels/111/0",
                    "https://discord.com/channels/111/" + "9" * 5000):
            with self.subTest(bad=bad[:100]), self.assertRaises(ValueError):
                sc.parse_channel_link(bad)

    def test_image_url_allowlist_rejects_navigation_and_local_resources(self):
        for bad in ("javascript:document.title=123", "file:///C:/example.png",
                    "http://127.0.0.1:8000/private", "https://attacker.invalid/page.html",
                    "https://evildiscordapp.net/attachments/222/777/image.png",
                    "https://cdn.discordapp.com:444/attachments/222/777/image.png",
                    "https://user@cdn.discordapp.com/attachments/222/777/image.png",
                    "https://cdn.discordapp.com/login", CDN_URL + "#fragment"):
            self.assertIsNone(sc._image_url(bad))
            self.assertIsNone(sc._full_size_url(dict(url=bad)))
        self.assertIsNotNone(sc._image_url(CDN_URL))
        self.assertIsNotNone(sc._image_url("https://images-ext-1.discordapp.net/external/hash/https/example.invalid/x.png"))

    def test_full_size_handles_malformed_dimensions_without_untrusted_navigation(self):
        self.assertEqual(sc._full_size_url(dict(url=CDN_URL, w="invalid", h=None)), CDN_URL)
        self.assertEqual(sc._full_size_url(dict(url=CDN_URL, w=float("nan"), h={})), CDN_URL)


class PacerTests(unittest.IsolatedAsyncioTestCase):
    async def test_nap_stops_at_budget(self):
        pacer = sc.Pacer(threading.Event(), max_minutes=0.0005)
        started = time.monotonic()
        self.assertFalse(await pacer.nap(100))
        self.assertEqual(pacer.outcome(), sc.BUDGET)
        self.assertLess(time.monotonic() - started, 0.5)

    async def test_stop_interrupts_running_operation(self):
        stopped = threading.Event()
        pacer = sc.Pacer(stopped)
        cancelled = asyncio.Event()

        async def pending():
            try:
                await asyncio.sleep(100)
            finally:
                cancelled.set()

        task = asyncio.create_task(pacer.run(pending()))
        await asyncio.sleep(0.01)
        stopped.set()
        with self.assertRaises(sc.PacerInterrupted) as caught:
            await task
        self.assertEqual(caught.exception.outcome, sc.STOPPED)
        self.assertTrue(cancelled.is_set())

    async def test_run_obeys_budget_and_operation_timeout(self):
        pacer = sc.Pacer(threading.Event(), max_minutes=0.0005)
        with self.assertRaises(sc.PacerInterrupted) as caught:
            await pacer.run(asyncio.sleep(100))
        self.assertEqual(caught.exception.outcome, sc.BUDGET)
        pacer = sc.Pacer(threading.Event())
        with self.assertRaises(asyncio.TimeoutError):
            await pacer.run(asyncio.sleep(100), timeout=0.02)

    async def test_paused_time_extends_budget(self):
        pause = threading.Event()
        pause.set()
        pacer = sc.Pacer(threading.Event(), pause, max_minutes=0.01, log=lambda _: None)
        before = pacer.deadline
        task = asyncio.create_task(pacer.nap(0))
        await asyncio.sleep(0.02)
        pause.clear()
        self.assertTrue(await task)
        self.assertGreater(pacer.deadline - before, 0.02)

    def test_nonfinite_or_negative_budget_rejected(self):
        for value in (-1, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                sc.Pacer(threading.Event(), max_minutes=value)


class CaptureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Store(self.root / "synthetic.db")
        self.pacer = sc.Pacer(threading.Event())
        self.ses = sc.Session(profile_dir=self.root / "unused", store=self.db,
                              pacer=self.pacer, log=lambda _: None,
                              images_dir=self.root / "images")

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    async def test_lookalike_and_query_origins_cannot_poison_archive(self):
        for url in ("https://evildiscord.com/api/v9/channels/222/messages?before=555",
                    "https://example.invalid/?url=https://discord.com/api/v9/channels/222/messages?before=555",
                    "https://discord.com/api/v9/channels/222/messages/extra",
                    "http://discord.com/api/v9/channels/222/messages",
                    "https://discord.com/api/v9/guilds/111/messages/search/extra"):
            r = response(url, [message()])
            await self.ses._on_response(r)
            r.json.assert_not_awaited()
        self.assertIsNone(self.db.get("222", "444"))

    async def test_real_response_records_request_identity_and_bounds(self):
        r = response("https://discord.com/api/v9/channels/222/messages?around=444&limit=50",
                     [message("443"), message("444"), message("445")])
        await self.ses._on_response(r)
        self.assertIsNotNone(self.db.get("222", "444"))
        captured = self.ses.fetches[-1]
        self.assertEqual(captured["ids"], (443, 444, 445))
        self.assertEqual(captured["query"]["around"], "444")
        self.assertEqual(captured["request_target"], {"kind": "around", "id": 444})
        self.assertEqual((captured["coverage_lo"], captured["coverage_hi"]), (443, 445))
        self.assertFalse(captured["at_start"])
        self.assertFalse(captured["at_end"])

    async def test_non_get_search_is_ignored(self):
        r = response("https://discord.com/api/v9/guilds/111/messages/search", {})
        r.request.method = "POST"
        await self.ses._on_response(r)
        r.json.assert_not_awaited()

    async def test_search_request_timestamp_is_issue_time_not_response_time(self):
        r = response("https://discord.com/api/v9/guilds/111/messages/search?author_id=333",
                     {"messages": [[message(channel_id="222", hit=True)]], "total_results": 1})
        r.request = Mock(method="GET")
        started = time.monotonic()
        self.ses._note_request(r.request)
        recorded = self.ses._request_starts[r.request]
        await asyncio.sleep(0.001)
        await self.ses._on_response(r)
        self.assertEqual(self.ses.searches[-1]["request_started"], recorded)
        self.assertGreaterEqual(recorded, started)
        self.assertNotIn(r.request, self.ses._request_starts)

    async def test_untracked_search_has_no_fabricated_timestamp(self):
        r = response("https://discord.com/api/v9/guilds/111/messages/search?author_id=333",
                     {"messages": [], "total_results": 0})
        await self.ses._on_response(r)
        self.assertIsNone(self.ses.searches[-1]["request_started"])

    async def test_ambiguous_or_malformed_batch_does_not_claim_coverage(self):
        for suffix, data in (("?before=555&around=444", [message()]),
                             ("?limit=0", [message()]),
                             ("?before=555", [message("bad")] )):
            await self.ses._on_response(response(
                "https://discord.com/api/v9/channels/222/messages" + suffix, data))
        self.assertEqual(self.db.coverage("222"), [])

    async def test_login_wait_returns_when_budget_expires(self):
        self.ses.pacer = sc.Pacer(threading.Event(), max_minutes=0.0005)
        self.ses.page = types.SimpleNamespace(goto=AsyncMock(), url="https://discord.com/login")
        self.assertFalse(await self.ses.open_app())
        self.assertEqual(self.ses.pacer.outcome(), sc.BUDGET)

    async def test_browser_startup_is_budget_bounded_and_stops_driver(self):
        self.ses.pacer = sc.Pacer(threading.Event(), max_minutes=0.0005)
        driver = Mock()
        driver.stop = AsyncMock()
        driver.chromium.launch_persistent_context = AsyncMock(side_effect=lambda **_: asyncio.sleep(100))
        # AsyncMock returning a coroutine does not await it, so use a real
        # coroutine side effect to represent the pending Playwright launch.
        async def launch(**_):
            await asyncio.sleep(100)
        driver.chromium.launch_persistent_context.side_effect = launch
        manager = Mock(start=AsyncMock(return_value=driver))
        with patch.object(sc, "async_playwright", return_value=manager):
            with self.assertRaises(sc.PacerInterrupted) as caught:
                await self.ses.__aenter__()
        self.assertEqual(caught.exception.outcome, sc.BUDGET)
        driver.stop.assert_awaited_once()

    async def test_cleanup_timeout_does_not_mask_outcome_or_hang(self):
        async def blocked():
            await asyncio.sleep(100)
        self.ses.context = Mock(close=AsyncMock(side_effect=blocked))
        self.ses._pw = Mock(stop=AsyncMock(side_effect=blocked))
        started = time.monotonic()
        with patch.object(sc, "CLEANUP_TIMEOUT_S", 0.01):
            self.assertFalse(await self.ses.__aexit__(sc.PacerInterrupted,
                sc.PacerInterrupted(sc.BUDGET), None))
        await asyncio.sleep(0)
        self.assertLess(time.monotonic() - started, 0.5)
        self.ses._pw.stop.assert_awaited_once()

    async def test_channel_job_reports_budget_during_session_start(self):
        manager = Mock()
        manager.__aenter__ = AsyncMock(side_effect=sc.PacerInterrupted(sc.BUDGET))
        manager.__aexit__ = AsyncMock(return_value=False)
        with patch.object(sc, "Session", return_value=manager), \
             patch.object(sc, "window_ids", return_value=(100, 500)):
            result = await sc.run_scrape(guild_id="111", channel_id="222", start_dt=None,
                end_dt=None, out_path=self.root / "out.json", profile_dir=self.root / "unused",
                log=lambda _: None, stop_event=threading.Event(), store=self.db)
        self.assertEqual(result["outcome"], sc.BUDGET)
        self.assertEqual(result["count"], 0)

    def image(self, mid="446", name="image.png"):
        self.db.put_messages("222", [dict(id=mid, content="", attachments=[
            dict(id="777", name=name, ctype="image/png", url=CDN_URL)])])
        return self.db.images_for("222", mid)[0]

    async def test_late_thumbnail_cannot_overwrite_full_image(self):
        row = self.image()
        started, release = asyncio.Event(), asyncio.Event()

        async def delayed(_):
            started.set()
            await release.wait()
            return b"thumbnail", "image/png"

        self.ses._download_image = delayed
        r = response(CDN_URL)
        r.headers = {"content-type": "image/png"}
        task = asyncio.create_task(self.ses._take_image(r))
        await started.wait()
        self.assertTrue(self.ses._save_image(row, b"larger full-size image", "image/png", True))
        release.set()
        await task
        final = self.db.image(row["key"])
        self.assertEqual(final["full"], 1)
        self.assertEqual(Path(final["file"]).read_bytes(), b"larger full-size image")

    async def test_forwarded_attachment_keeps_both_message_files(self):
        self.image("446")
        self.image("447")
        self.ses._download_image = AsyncMock(return_value=(b"synthetic", "image/png"))
        r = response(CDN_URL)
        r.headers = {"content-type": "image/png"}
        await self.ses._take_image(r)
        rows = self.db.images_by_attachment("777")
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({r["file"] for r in rows}), 2)
        self.assertTrue(all(Path(r["file"]).read_bytes() == b"synthetic" for r in rows))

    def test_filename_traversal_and_invalid_channel_cannot_escape_images(self):
        row = self.image(name="../../escape.png")
        self.assertTrue(self.ses._save_image(row, b"synthetic", "image/png", False))
        self.assertTrue(Path(self.db.image(row["key"])["file"]).is_relative_to(self.root / "images"))
        bad = dict(row, channel_id="../../escape")
        self.assertFalse(self.ses._save_image(bad, b"bad", "image/png", True))

    async def test_full_download_never_opens_browser_page_and_repairs_missing_file(self):
        row = self.image()
        self.ses._save_image(row, b"previous full bytes", "image/png", True)
        current = self.db.image(row["key"])
        Path(current["file"]).unlink()
        self.ses.context = Mock()
        self.ses._download_image = AsyncMock(return_value=(b"full", "image/png"))
        self.pacer.step = AsyncMock(return_value=True)
        self.assertEqual(await self.ses.fetch_full_images([current]), 1)
        self.ses.context.new_page.assert_not_called()
        self.assertEqual(Path(self.db.image(row["key"])["file"]).read_bytes(), b"full")

    async def test_download_stop_returns_promptly_and_cancels_daemon_worker(self):
        entered, exited = threading.Event(), threading.Event()
        workers = []

        def blocked(url, limit, deadline, cancel):
            workers.append(threading.current_thread())
            entered.set()
            cancel.wait(1)
            exited.set()
            raise TimeoutError("cancelled synthetic download")

        with patch.object(sc, "_read_cdn_image", side_effect=blocked):
            task = asyncio.create_task(self.ses._download_image(CDN_URL))
            while not entered.is_set():
                await asyncio.sleep(0.001)
            self.pacer.stop_event.set()
            started = time.monotonic()
            self.assertIsNone(await task)
            self.assertLess(time.monotonic() - started, 0.5)
            for _ in range(100):
                if exited.is_set():
                    break
                await asyncio.sleep(0.001)
            self.assertTrue(exited.is_set())
            self.assertTrue(workers[0].daemon)

    async def test_session_quota_is_enforced_before_network(self):
        self.ses._image_bytes = sc.MAX_SESSION_IMAGE_BYTES
        with patch.object(sc, "_read_cdn_image") as network:
            self.assertIsNone(await self.ses._download_image(CDN_URL))
            network.assert_not_called()

    async def test_worker_count_capped_when_resolver_is_stuck(self):
        entered, release = threading.Event(), threading.Event()
        calls = []

        def blocked(url, limit, deadline, cancel):
            calls.append(url)
            entered.set()
            release.wait(1)
            return b"synthetic", "image/png"

        other = sc.Session(profile_dir=self.root / "unused", store=self.db,
            pacer=sc.Pacer(threading.Event()), log=lambda _: None, images_dir=self.root / "images")
        with patch.object(sc, "_IMAGE_WORKERS", threading.BoundedSemaphore(1)), \
             patch.object(sc, "_read_cdn_image", side_effect=blocked):
            task = asyncio.create_task(self.ses._download_image(CDN_URL))
            try:
                while not entered.is_set():
                    await asyncio.sleep(0.001)
                self.assertIsNone(await other._download_image(CDN_URL))
                self.assertEqual(len(calls), 1)
            finally:
                release.set()
                await task


class StreamTests(unittest.TestCase):
    def read(self, data=b"image bytes", status=200, headers=None, limit=100):
        stream = io.BytesIO(data)
        r = types.SimpleNamespace(status=status, fp=None,
            getheader=lambda k, default=None: (headers or {"content-type": "image/png"}).get(k, default),
            read1=Mock(side_effect=stream.read1))
        connection = Mock()
        connection.getresponse.return_value = r
        with patch.object(sc.http.client, "HTTPSConnection", return_value=connection) as ctor:
            try:
                result = sc._read_cdn_image(CDN_URL, limit, time.monotonic() + 5, threading.Event())
                return result, connection, ctor, r
            except Exception:
                self.last_response = r
                self.last_connection = connection
                raise

    def test_stream_uses_no_cookie_auth_or_redirect_mechanism(self):
        result, conn, ctor, r = self.read()
        self.assertEqual(result, (b"image bytes", "image/png"))
        self.assertEqual(ctor.call_args.args, ("cdn.discordapp.com", 443))
        headers = conn.request.call_args.kwargs["headers"]
        self.assertNotIn("Cookie", headers)
        self.assertNotIn("Authorization", headers)
        self.assertEqual(headers["Accept-Encoding"], "identity")
        conn.close.assert_called_once()

    def test_redirect_is_rejected_without_reading_body(self):
        with self.assertRaises(ValueError):
            self.read(status=302)
        self.last_response.read1.assert_not_called()
        self.last_connection.close.assert_called_once()

    def test_svg_html_encoded_and_large_headers_are_rejected(self):
        for headers in ({"content-type": "image/svg+xml"}, {"content-type": "text/html"},
                        {"content-type": "image/png", "content-encoding": "gzip"},
                        {"content-type": "image/png", "content-length": "101"}):
            with self.subTest(headers=headers), self.assertRaises(ValueError):
                self.read(headers=headers)
            self.last_response.read1.assert_not_called()

    def test_body_without_length_cannot_exceed_limit(self):
        with self.assertRaises(ValueError):
            self.read(data=b"x" * 101, limit=100)

    def test_truncated_response_rejected(self):
        with self.assertRaises(ValueError):
            self.read(data=b"short", headers={"content-type": "image/png", "content-length": "10"})

    def test_expired_or_cancelled_download_never_connects(self):
        for cancelled, deadline in ((False, time.monotonic() - 1), (True, time.monotonic() + 5)):
            event = threading.Event()
            if cancelled:
                event.set()
            with patch.object(sc.http.client, "HTTPSConnection") as conn, self.assertRaises(TimeoutError):
                sc._read_cdn_image(CDN_URL, 100, deadline, event)
            conn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
