import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch
from datetime import datetime, timezone

from app import exports, gui, jobs, persistence


class Var:
    def __init__(self, value=""):
        self.value = value
    def get(self):
        return self.value
    def set(self, value):
        self.value = value


class GuiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.app = object.__new__(gui.ScraperApp)
        self.app.jobs = []
        self.app.batches = []
        self.app.settings = {}
        self.app.logs = []
        self.app._log = self.app.logs.append
        for name in ("_save_batches", "_save_settings", "_refresh_tree", "_save_queue",
                     "_refresh_batch_info"):
            setattr(self.app, name, lambda: None)
        self.prompt = patch.object(gui.messagebox, "askyesno", return_value=False).start()
        self.addCleanup(patch.stopall)

    def job(self, channel="22", status=jobs.PENDING, kind=jobs.CHANNEL_JOB):
        job = jobs.new_job(kind, label="general", auto_label=False, guild_id="11",
                           channel_id=channel, start="2026-11-10", end="2026-01-01",
                           out=str(self.folder / "shared.json"), auto_out=False,
                           username="target", server="server")
        job["status"] = status
        return job

    def test_every_resumable_status_reserves_output(self):
        for status in jobs.RESUMABLE:
            with self.subTest(status=status):
                self.app.jobs = [self.job(status=status)]
                with self.assertRaises(ValueError):
                    self.app._check_free(self.folder / "shared.json")

    @unittest.skipUnless(gui.os.name == "nt", "Windows filesystem semantics")
    def test_case_alias_reserves_output(self):
        self.app.jobs = [self.job()]
        with self.assertRaises(ValueError):
            self.app._check_free(self.folder / "SHARED.JSON")

    def test_companion_markdown_is_reserved(self):
        self.app.jobs = [self.job(kind=jobs.USER_JOB)]
        with self.assertRaises(ValueError):
            self.app._check_free(self.folder / "shared.md")

    def test_unrelated_existing_file_needs_approval(self):
        job = self.job()
        Path(job["out"]).write_text("unrelated data", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.app._prepare_output(job)
        self.assertEqual(Path(job["out"]).read_text(), "unrelated data")
        self.prompt.return_value = True
        self.app._prepare_output(job)
        self.assertEqual(job["output_hashes"][exports.path_key(job["out"])],
                         exports.fingerprint(job["out"]))

    def test_auto_files_are_unique_and_never_replace_unrelated_file(self):
        first = self.job()
        first["auto_out"] = True
        Path(first["out"]).write_text("unrelated", encoding="utf-8")
        self.app._prepare_output(first)
        self.app.jobs.append(first)
        second = self.job("33")
        second["auto_out"] = True
        self.app._prepare_output(second)
        self.assertNotEqual(first["out"], second["out"])
        self.assertEqual((self.folder / "shared.json").read_text(), "unrelated")

    def test_three_batch_channels_with_same_label_and_suffix_are_unique(self):
        batch = {"name": "test", "channels": [
            {"guild_id": "11", "channel_id": channel, "label": "general"}
            for channel in ("110000000000001234", "220000000000001234", "330000000000001234")]}
        self.app.batches = [batch]
        self.app._current_batch = lambda: batch
        self.app._label_for = lambda g, c, label: (label, False)
        self.app.start_var, self.app.end_var = Var("2026-11-10"), Var("2026-01-01")
        self.app.out_var, self.app.dated_var = Var(str(self.folder)), Var(False)
        self.app._queue_batch()
        self.assertEqual(len({j["out"] for j in self.app.jobs}), 3)
        self.assertNotIn("last_queued", batch)
        self.assertEqual(self.app._batch_success_date(batch), "")

    def test_legacy_enqueue_time_does_not_advance_dates(self):
        batch = {"name": "test", "channels": [{"channel_id": "22"}],
                 "last_queued": "2026-11-11"}
        self.assertEqual(self.app._batch_success_date(batch), "")

    def test_completed_baseline_advances_only_successful_channels(self):
        batch = {"id": "batch1", "name": "test", "channels": [
            {"channel_id": "22"}, {"channel_id": "33"}],
            "requested_from": {"22": "2026-01-01", "33": "2026-01-01"}}
        self.app.batches = [batch]
        when = datetime(2026, 11, 11, tzinfo=timezone.utc)
        first = self.job()
        first["batch_id"] = batch["id"]
        self.app._record_batch_success(first, when)
        self.assertEqual(self.app._batch_success_date(batch), "")
        second = self.job("33")
        second["batch_id"] = batch["id"]
        self.app._record_batch_success(second, when)
        self.assertEqual(self.app._batch_success_date(batch), "2026-11-10")
        too_short = self.job()
        too_short.update(batch_id="batch1", end="2026-11-11", start="")
        self.app._record_batch_success(too_short, when)
        self.assertEqual(batch["completed"]["22"], "2026-11-10")

    def test_clear_finished_keeps_failed_and_incomplete_jobs(self):
        self.app.jobs = [self.job(status=s) for s in
                         (jobs.DONE, jobs.FAILED, jobs.STOPPED, jobs.INCOMPLETE)]
        self.app._clear_finished()
        self.assertEqual([j["status"] for j in self.app.jobs],
                         [jobs.FAILED, jobs.STOPPED, jobs.INCOMPLETE])

    def test_malformed_settings_shapes_fall_back_with_diagnostic(self):
        path = self.folder / "settings.json"
        with patch.object(gui, "SETTINGS_FILE", path):
            for value in (None, [], "text", 42, {"keep_awake": []}):
                path.write_text(json.dumps(value))
                self.assertEqual(self.app._load_settings(), {})
        self.assertTrue(self.app.logs)

    def test_pair_rename_rolls_back_if_markdown_is_locked(self):
        job = self.job(kind=jobs.USER_JOB)
        job["auto_out"] = True
        for path in jobs.output_paths(job):
            path.write_text("owned export", encoding="utf-8")
        job["output_hashes"] = {exports.path_key(p): exports.fingerprint(p)
                                for p in jobs.output_paths(job)}
        before = job["out"]
        original_rename = Path.rename
        def fail_markdown(path, target):
            if path.suffix == ".md":
                raise PermissionError("simulated locked Markdown")
            return original_rename(path, target)
        with patch.object(Path, "rename", fail_markdown):
            self.app._sync_out(job)
        self.assertEqual(job["out"], before)
        self.assertTrue(all(p.exists() for p in jobs.output_paths(job)))

    def test_dates_too_recent_to_search_stay_resumable_but_unknown_user_fails(self):
        status, note = jobs.outcome_status(self.job(kind=jobs.USER_JOB),
            {"outcome": gui.scraper_core.STALLED, "deferred_recent": True,
             "searched_any": False, "author_id": None, "note": "Indexing pending"})
        self.assertEqual(status, jobs.INCOMPLETE)
        self.assertIn("Indexing pending", note)
        # A search that did run and found no such person is a failure, also
        # for a job "up to now" (which always leaves its last minutes for later).
        status, note = jobs.outcome_status(self.job(kind=jobs.USER_JOB),
            {"outcome": gui.scraper_core.STALLED, "deferred_recent": True,
             "searched_any": True, "author_id": None, "note": "No user called 'x'"})
        self.assertEqual(status, jobs.FAILED)
        self.assertIn("No user", note)

    def test_job_up_to_now_is_done(self):
        status, _note = jobs.outcome_status(self.job(kind=jobs.USER_JOB),
            {"outcome": gui.scraper_core.END_DATE, "deferred_recent": True,
             "searched_any": True, "author_id": "30", "count": 3, "channels": 1})
        self.assertEqual(status, jobs.DONE)

    def batch_app(self, batch):
        self.app.batches = [batch]
        self.app._current_batch = lambda: batch
        self.app._label_for = lambda g, c, label: (label or "chan", False)
        self.app.out_var, self.app.dated_var = Var(str(self.folder)), Var(False)
        self.prompt.return_value = True

    def finish_all(self, day):
        for job in list(self.app.jobs):
            job["status"] = jobs.DONE
            self.app._record_batch_success(dict(job), datetime(2026, 11, day, 12, tzinfo=timezone.utc))
        self.app.jobs.clear()

    def test_batch_from_an_older_version_still_offers_since_last_time(self):
        batch = {"name": "old", "last_queued": "2026-11-09", "channels": [
            {"guild_id": "11", "channel_id": "22", "label": "a"},
            {"guild_id": "11", "channel_id": "33", "label": "b"}]}
        self.batch_app(batch)
        self.assertEqual(self.app._batch_since(batch), ("2026-11-09", "last queued"))
        self.app.start_var, self.app.end_var = Var(""), Var("2026-11-09")
        self.app._queue_batch()
        self.finish_all(12)
        self.assertEqual(self.app._batch_since(batch), ("2026-11-12", "collected through"))

    def test_one_mistaken_blank_end_date_does_not_stick(self):
        batch = {"name": "b", "channels": [{"guild_id": "11", "channel_id": "22", "label": "a"}]}
        self.batch_app(batch)
        self.app.start_var, self.app.end_var = Var(""), Var("")
        self.app._queue_batch()                 # blank End, confirmed...
        self.app.jobs.clear()                   # ...then removed without running
        offered = []
        for end, day in (("2026-11-01", 5), ("2026-11-05", 8), ("2026-11-08", 11)):
            self.app.start_var, self.app.end_var = Var(""), Var(end)
            self.app._queue_batch()
            self.finish_all(day)
            offered.append(self.app._batch_success_date(batch))
        self.assertEqual(offered, ["2026-11-05", "2026-11-08", "2026-11-11"])

    def test_failed_channel_holds_the_batch_date_back(self):
        batch = {"name": "b", "channels": [
            {"guild_id": "11", "channel_id": "22", "label": "a"},
            {"guild_id": "11", "channel_id": "33", "label": "b"}]}
        self.batch_app(batch)
        self.app.start_var, self.app.end_var = Var(""), Var("2026-11-01")
        self.app._queue_batch()
        self.finish_all(5)
        self.app.end_var = Var("2026-11-05")
        self.app._queue_batch()
        failed = next(j for j in self.app.jobs if j["channel_id"] == "33")
        self.app.jobs.remove(failed)            # channel 33 did not finish this time
        self.finish_all(9)
        self.assertEqual(self.app._batch_success_date(batch), "2026-11-05")
        self.assertEqual(batch["completed"], {"22": "2026-11-09", "33": "2026-11-05"})

    def test_older_or_newer_separate_range_does_not_move_the_date(self):
        batch = {"id": "b1", "name": "b", "channels": [{"guild_id": "11", "channel_id": "22"}],
                 "completed": {"22": "2026-11-05"}, "covered_from": {"22": "2026-11-01"}}
        self.app.batches = [batch]
        when = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)
        older = self.job()
        older.update(batch_id="b1", end="2026-01-01", start="2026-03-01")
        self.app._record_batch_success(older, when)
        newer = self.job()
        newer.update(batch_id="b1", end="2026-11-10", start="")
        self.app._record_batch_success(newer, when)
        self.assertEqual(batch["completed"], {"22": "2026-11-05"})
        joined = self.job()
        joined.update(batch_id="b1", end="2026-11-05", start="")
        self.app._record_batch_success(joined, when)
        self.assertEqual(batch["completed"], {"22": "2026-11-20"})
        self.assertEqual(batch["covered_from"], {"22": "2026-11-01"})

    def test_last_queued_day_is_checked_against_the_archive(self):
        day = "2026-11-09"

        def at(text):
            return gui.scraper_core.dt_to_snowflake(
                datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc))

        store = gui.Store(self.folder / "archive.db")
        self.addCleanup(store.close)
        store.add_coverage("22", at("2026-10-01"), at("2026-11-10"))      # reaches the day
        store.add_coverage("33", at("2026-10-01"), at("2026-11-06"))      # stops three days short
        batch = {"name": "old", "last_queued": day, "channels": [
            {"guild_id": "11", "channel_id": c, "label": n}
            for c, n in (("22", "busy"), ("33", "short"), ("44", "never"))]}
        self.batch_app(batch)
        self.app.store = store
        self.assertEqual(self.app._legacy_gaps(batch, day),
                         [("short", "2026-11-06"), ("never", None)])
        self.assertEqual(self.app._legacy_gaps(batch, "not a date"), [])
        # The date offered is still the one 1.0.0 offered; the gap is said out loud.
        self.assertEqual(self.app._batch_since(batch), (day, "last queued"))
        self.app.start_var, self.app.end_var = Var("x"), Var()
        self.app._prefill_batch_dates()
        self.assertEqual((self.app.start_var.get(), self.app.end_var.get()), ("", day))
        note = next(line for line in self.app.logs if line.startswith("NOTE:"))
        self.assertIn("short (since 2026-11-06)", note)
        self.assertIn("never (nothing before)", note)
        self.assertNotIn("busy", note)
        # Once a run with this version has finished, its own record is used
        # and the old day, with its warning, is no longer shown.
        self.app.logs.clear()
        batch["completed"] = {"22": "2026-11-12", "33": "2026-11-12", "44": "2026-11-12"}
        self.app._prefill_batch_dates()
        self.assertEqual(self.app.end_var.get(), "2026-11-12")
        self.assertFalse(any(line.startswith("NOTE:") for line in self.app.logs))

    def test_job_just_after_midnight_counts_only_the_day_before(self):
        # The engine stops a minute short of "now". At 00:00:20 UTC that is
        # still yesterday, and yesterday's last minute is not collected yet.
        just_after = datetime(2026, 11, 12, 0, 0, 20, tzinfo=timezone.utc)
        for through in ("2026-11-11", None):          # the engine's answer; worked out here
            with self.subTest(through=through):
                batch = {"id": "b1", "name": "b",
                         "channels": [{"guild_id": "11", "channel_id": "22"}]}
                self.app.batches = [batch]
                job = self.job()
                job.update(batch_id="b1", end="2026-11-09", start="")
                self.app._record_batch_success(dict(job), just_after, through)
                self.assertEqual(batch["completed"], {"22": "2026-11-11"})
                self.assertEqual(self.app._batch_success_date(batch), "2026-11-11")
        # A minute later the job's dates reach into the new day.
        batch = {"id": "b1", "name": "b", "channels": [{"guild_id": "11", "channel_id": "22"}]}
        self.app.batches = [batch]
        self.app._record_batch_success(dict(job), datetime(2026, 11, 12, 0, 1, 30, tzinfo=timezone.utc))
        self.assertEqual(batch["completed"], {"22": "2026-11-12"})

    def test_finished_job_does_not_hold_its_file(self):
        done, waiting = self.job(status=jobs.DONE), self.job(status=jobs.PENDING)
        failed = self.job(status=jobs.FAILED)
        self.app.jobs = [done, failed, waiting]
        self.app._check_free(waiting["out"], kind=waiting["type"], ignore=waiting)
        self.app.jobs.append(self.job(status=jobs.STOPPED))
        with self.assertRaises(ValueError):
            self.app._check_free(waiting["out"], kind=waiting["type"], ignore=waiting)

    def test_one_odd_value_does_not_cost_the_saved_batches(self):
        path = self.folder / "batches.json"
        original = [
            {"name": "keep me", "last_queued": "2026-11-09", "channels": [
                {"guild_id": 11, "channel_id": "22", "label": None},
                {"guild_id": "x", "channel_id": "33", "label": "unusable"}]},
            {"name": "and me", "completed": "not a mapping",
             "channels": [{"guild_id": "11", "channel_id": "33", "label": "x"}]},
            {"no": "name"}]
        path.write_text(json.dumps(original), encoding="utf-8")
        real_save = gui.ScraperApp._save_batches
        with patch.object(gui, "BATCHES_FILE", path):
            loaded = self.app._load_batches()
            self.assertEqual([b["name"] for b in loaded], ["keep me", "and me"])
            self.assertEqual(loaded[0]["channels"],
                             [{"guild_id": "11", "channel_id": "22", "label": ""}])
            self.assertEqual(loaded[0]["last_queued"], "2026-11-09")
            self.assertNotIn("completed", loaded[1])
            self.assertTrue(any("could not be read" in line for line in self.app.logs))
            self.app.batches = loaded
            real_save(self.app)
        self.assertEqual(len(json.loads(path.read_text(encoding="utf-8"))), 2)
        # The file exactly as it was is set aside, not lost.
        self.assertEqual(json.loads((self.folder / "batches.json.rejected").read_text(encoding="utf-8")),
                         original)

    def test_invalid_queue_entries_do_not_crash_loader(self):
        for value in (None, [], 1, {"guild_id": "1", "channel_id": "2", "out": 42},
                      {"guild_id": "../x", "channel_id": "2", "out": "out.json"}):
            self.assertFalse(jobs.fill_job(value))


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"

    def test_atomic_write_failure_keeps_original_and_backup(self):
        persistence.save(self.path, {"value": 1})
        real_replace = persistence.os.replace
        def fail_primary(src, dst):
            if Path(dst) == self.path:
                raise OSError("simulated disk failure")
            return real_replace(src, dst)
        with patch.object(persistence.os, "replace", side_effect=fail_primary):
            with self.assertRaises(OSError):
                persistence.save(self.path, {"value": 2})
        self.assertEqual(json.loads(self.path.read_text()), {"value": 1})
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_corrupt_primary_recovers_last_good_backup(self):
        persistence.save(self.path, {"value": 1})
        persistence.save(self.path, {"value": 2})
        self.path.write_text("{broken")
        warnings = []
        self.assertEqual(persistence.load(self.path, lambda x: isinstance(x, dict), {},
                                          warnings.append), {"value": 1})
        self.assertIn("Recovered", warnings[-1])

    def test_invalid_shape_does_not_replace_last_good_backup(self):
        persistence.save(self.path, {"value": 1})
        persistence.save(self.path, {"value": 2})
        self.path.write_text("null")
        persistence.save(self.path, {"value": 3})
        self.assertEqual(json.loads(self.path.with_suffix(".json.bak").read_text()),
                         {"value": 1})
        # ...and what could not be understood is set aside rather than lost.
        self.assertEqual(self.path.with_suffix(".json.rejected").read_text(), "null")
        self.assertEqual(json.loads(self.path.read_text()), {"value": 3})

    def test_export_guard_preserves_file_changed_during_collection(self):
        self.path.write_text("original", encoding="utf-8")
        expected = {exports.path_key(self.path): exports.fingerprint(self.path)}
        with exports.authorized(expected):
            self.path.write_text("external edit", encoding="utf-8")
            with self.assertRaises(ValueError):
                exports.write_text(self.path, "new export")
        self.assertEqual(self.path.read_text(), "external edit")

    def test_export_guard_tracks_its_own_writes(self):
        expected = {exports.path_key(self.path): None}
        updates = []
        with exports.authorized(expected, updates.append):
            exports.write_text(self.path, "first")
            exports.write_text(self.path, "second")
        self.assertEqual(self.path.read_text(), "second")
        self.assertEqual(updates[-1][exports.path_key(self.path)], exports.fingerprint(self.path))


if __name__ == "__main__":
    unittest.main()
