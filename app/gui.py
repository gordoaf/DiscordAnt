"""DiscordAnt — desktop GUI with a job queue.

Two kinds of job go in the queue: a channel over a date range, and one user's
messages over a date range (with the conversation around each one). Press
Start queue and the app works through them one at a time in a real Chromium
window at human pace. Windows sleep/screensaver stay off for the whole run, so
a queue can be left to grind overnight.

Everything collected goes into one archive on this PC, and a job only scrolls
through what the archive doesn't hold yet. So a job that was stopped carries
on where it left off, and nothing is scraped twice.

Channels that get scraped over and over can be saved as a named *batch*: pick
the batch, give it a date range, and one press queues a job per channel.
"""

import asyncio
import json
import os
import queue
import random
import shutil
import sys
import threading
import time
import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

# Where the browser is. This has to be settled BEFORE playwright is imported.
#
# An installed copy carries its own Chromium in a "browser" folder beside the
# exe (build.py puts it there), so it works on a PC that has nothing else set
# up. A build made without it falls back to Playwright's shared browser
# folder. Running from source needs nothing here: Playwright finds the browser
# that `playwright install chromium` downloaded.
if getattr(sys, "frozen", False):
    _bundled = Path(sys.executable).resolve().parent / "browser"
    if _bundled.is_dir():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(_bundled)
    elif not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        _cache = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ms-playwright"
        if _cache.exists():
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(_cache)

try:
    from . import jobs, keep_awake, scraper_core, user_history
    from .appinfo import APP_NAME, HOMEPAGE, __version__
    from .jobs import (CHANNEL_JOB, DONE, FAILED, INCOMPLETE, PENDING, RUNNING,
                       STOPPED, USER_JOB)
    from .scraper_core import parse_channel_link, run_scrape
    from .store import Store
except ImportError:  # running as a plain script / frozen exe
    import jobs
    import keep_awake
    import scraper_core
    import user_history
    from appinfo import APP_NAME, HOMEPAGE, __version__
    from jobs import (CHANNEL_JOB, DONE, FAILED, INCOMPLETE, PENDING, RUNNING,
                      STOPPED, USER_JOB)
    from scraper_core import parse_channel_link, run_scrape
    from store import Store

# Browser profile, settings, the saved queue and the archive live outside the
# program folder, so updating or reinstalling the app never touches them.
DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / APP_NAME
DATA_DIR.mkdir(parents=True, exist_ok=True)
PROFILE_DIR = DATA_DIR / "chrome_profile"
SETTINGS_FILE = DATA_DIR / "gui_settings.json"
QUEUE_FILE = DATA_DIR / "queue.json"
BATCHES_FILE = DATA_DIR / "batches.json"   # named channel sets, see BatchEditor
LOG_FILE = DATA_DIR / "scraper.log"   # survives the app closing; for diagnosis
ARCHIVE_DB = DATA_DIR / "archive.db"  # every message ever collected + what is covered
IMAGES_DIR = DATA_DIR / "images"      # pictures saved from messages

# Pause between queued jobs — same spirit as the pacing inside a scrape.
JOB_PAUSE_MIN_S, JOB_PAUSE_MAX_S = 20, 60

PAD = {"padx": 8, "pady": 4}
ROW = {"padx": 6, "pady": 3}      # the tighter spacing of the one-page form
GREY = "#777"
ALL_SERVERS = "All servers in this list"

HELP_TEXT = """\
ADDING JOBS

Fill in the dates and where to save, then add what you want. There are three
kinds of job, one row each:

Channel — one channel. Right-click the channel in Discord, Copy Link, paste
it in. Leave the label blank and the channel's real name is used.

Batch — a saved set of channels. Pick the batch and one press queues a job
for every channel in it. Picking a batch fills in the end date with the day
it was last queued, so the default is "everything since last time".

User — every message one person posted in a server, grouped by channel, each
with the conversation around it and what it was replying to. You get a .json
file and a readable .md transcript. Use their username (the @name on their
profile), not their display name.


PICTURES

'Save pictures' keeps the pictures people post and lists each one, with its
place on disk, next to its message in the output file. 'Full-size' also
fetches full-size copies when a job finishes (slower; without it you keep the
smaller previews).


RUNNING

Start queue — works through the jobs one at a time.
Pause / Resume — holds everything where it is; the browser stays open.
Stop after this job — finishes the running job, then stops.
Stop now — stops straight away. Nothing is lost.


NOTHING IS SCRAPED TWICE

Everything collected is kept in one archive on this PC. A job only scrolls
through the parts of its date range the archive doesn't hold yet. So:

- A stopped or incomplete job carries on from where it left off the next time
  you press Start queue.
- Asking for dates you already have costs no scrolling: the file is simply
  written out from the archive.
- 'Run again' re-runs a finished job the same way (only what is missing).
- 'Start over' scrolls the whole range again. Use it to pick up edits, or
  pictures that were not saved the first time.


DATES

Start date = the newest day you want. Blank means up to now.
End date = the oldest day you want. Blank means back to the very beginning.


FIRST TIME

A browser window opens on Discord's own login page. Log in there once; the
login is remembered on this PC. The app never asks for your password.

To switch accounts, or to forget the login, use Tools > Log out of Discord.


PLEASE DON'T SPEED IT UP

The app scrolls at the pace of a person reading, so it asks no more of
Discord than a person would. A user-history job also types into Discord's
search box and opens old conversations, at the same unhurried pace.

That pace is not a promise of safety. Automating a personal account is
against Discord's terms, and using this app is at your own risk.
"""


class Tip:
    """A small hint that appears when the mouse rests on a widget."""

    def __init__(self, widget, text):
        self.widget, self.text = widget, text
        self.win = self.job = None
        widget.bind("<Enter>", self._arm, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _arm(self, _event=None):
        self._hide()
        self.job = self.widget.after(600, self._show)

    def _show(self):
        self.job = None
        try:
            x = self.widget.winfo_rootx() + 12
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
            self.win = tk.Toplevel(self.widget)
            self.win.wm_overrideredirect(True)
            self.win.wm_geometry(f"+{x}+{y}")
            tk.Label(self.win, text=self.text, justify="left", background="#ffffe1",
                     foreground="#222", relief="solid", borderwidth=1, padx=6,
                     pady=3, wraplength=440).pack()
        except tk.TclError:      # the widget went away while we waited
            self.win = None

    def _hide(self, _event=None):
        if self.job:
            try:
                self.widget.after_cancel(self.job)
            except tk.TclError:
                pass
            self.job = None
        if self.win:
            try:
                self.win.destroy()
            except tk.TclError:
                pass
            self.win = None


class ScraperApp:
    def __init__(self, root):
        self.root = root
        root.title(APP_NAME)
        root.geometry("960x780")
        root.minsize(860, 600)

        self.log_q = queue.Queue()
        self.ui_q = queue.Queue()   # callables the worker wants run on the main thread
        self.jobs = []
        self.worker = None
        self.stop_current = threading.Event()   # abandon the running job
        self.stop_queue = threading.Event()     # stop after the current job
        self.pause_event = threading.Event()    # hold everything where it is
        self.settings = self._load_settings()
        self.batches = self._load_batches()
        self.store = Store(ARCHIVE_DB)
        self.server_map = {}        # dropdown text -> (guild id, a channel in it)

        self._build_menu()
        self._build_ui()
        self._refresh_batches(select=self.settings.get("last_batch"))
        self._load_queue()
        self._apply_known_names(sync_files=True)
        self.root.after(150, self._drain_log)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------- UI ----------------
    def _build_menu(self):
        bar = tk.Menu(self.root)
        tools = tk.Menu(bar, tearoff=0)
        tools.add_command(label="Archive summary", command=self._archive_summary)
        tools.add_command(label="Open archive folder",
                          command=lambda: self._open_path(DATA_DIR))
        tools.add_command(label="Open pictures folder",
                          command=lambda: self._open_path(IMAGES_DIR, make=True))
        tools.add_command(label="Open log file",
                          command=lambda: self._open_path(LOG_FILE))
        tools.add_separator()
        tools.add_command(label="Log out of Discord...", command=self._log_out)
        bar.add_cascade(label="Tools", menu=tools)
        helpm = tk.Menu(bar, tearoff=0)
        helpm.add_command(label="Quick guide", command=self._show_help)
        helpm.add_command(label=f"About {APP_NAME}", command=self._show_about)
        bar.add_cascade(label="Help", menu=helpm)
        self.root.config(menu=bar)

    def _build_ui(self):
        outer = ttk.Frame(self.root)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=3)   # queue table
        outer.rowconfigure(3, weight=2)   # log
        self._build_form(outer)
        self._build_lower(outer)

    def _build_form(self, outer):
        """Everything that adds jobs, on one page: when, where, then one row
        for each kind of job with its own button at the end."""
        form = ttk.LabelFrame(outer, text="Add to the queue")
        form.grid(row=0, column=0, sticky="ew", padx=8, pady=(6, 2))
        form.columnconfigure(1, weight=1)

        def row(r, label, span=1):
            ttk.Label(form, text=label).grid(row=r, column=0, sticky="w", **ROW)
            box = ttk.Frame(form)
            box.grid(row=r, column=1, columnspan=span, sticky="ew", **ROW)
            return box

        def action(r, text, command, tip):
            btn = ttk.Button(form, text=text, command=command, width=13)
            btn.grid(row=r, column=2, sticky="e", padx=(2, 8), pady=3)
            Tip(btn, tip)

        # ---- when
        box = row(0, "Dates")
        ttk.Label(box, text="Start").pack(side="left")
        self.start_var = tk.StringVar()
        entry = ttk.Entry(box, textvariable=self.start_var, width=12)
        entry.pack(side="left", padx=(4, 6))
        Tip(entry, "The newest day wanted, as YYYY-MM-DD. Blank = up to now.")
        ttk.Label(box, text="newest day, blank = now", foreground=GREY).pack(side="left")
        ttk.Label(box, text="End").pack(side="left", padx=(16, 0))
        self.end_var = tk.StringVar()
        entry = ttk.Entry(box, textvariable=self.end_var, width=12)
        entry.pack(side="left", padx=(4, 6))
        Tip(entry, "The oldest day wanted, as YYYY-MM-DD. Blank = back to the "
                   "very beginning.")
        ttk.Label(box, text="oldest day, blank = the beginning",
                  foreground=GREY).pack(side="left")
        ttk.Label(box, text="YYYY-MM-DD", foreground=GREY).pack(side="right", padx=4)
        action(0, "Clear form", self._clear_form,
               "Empties the boxes in this panel. 'Save to' is kept.")

        # ---- where
        box = row(1, "Save to", span=2)
        self.out_var = tk.StringVar(value=self.settings.get("last_out_dir", ""))
        entry = ttk.Entry(box, textvariable=self.out_var)
        entry.pack(side="left", fill="x", expand=True)
        Tip(entry, "A folder: each job's file is named for you.\n"
                   "A .json path: names one job's file exactly.")
        ttk.Button(box, text="Folder...", command=self._browse_dir, width=9).pack(
            side="left", padx=(6, 2))
        ttk.Button(box, text="File...", command=self._browse_file, width=7).pack(
            side="left", padx=2)
        self.dated_var = tk.BooleanVar(value=self.settings.get("batch_dated_dir", False))
        tick = ttk.Checkbutton(box, variable=self.dated_var,
                               text="Batches into a dated subfolder")
        tick.pack(side="left", padx=(10, 2))
        Tip(tick, "Queue a batch into a subfolder of 'Save to' named for today "
                  "(for example 08-21).")

        ttk.Separator(form).grid(row=2, column=0, columnspan=3, sticky="ew",
                                 padx=6, pady=3)

        # ---- what: one channel
        box = row(3, "Channel link")
        self.link_var = tk.StringVar()
        entry = ttk.Entry(box, textvariable=self.link_var)
        entry.pack(side="left", fill="x", expand=True)
        Tip(entry, "Right-click the channel in Discord, choose Copy Link, paste it here.")
        ttk.Label(box, text="Label").pack(side="left", padx=(10, 4))
        self.label_var = tk.StringVar()
        entry = ttk.Entry(box, textvariable=self.label_var, width=22)
        entry.pack(side="left")
        Tip(entry, "Optional: a name for this channel, used in the file name.\n"
                   "Blank = the channel's real name.")
        action(3, "Add channel", self._add_job,
               "Queue this channel for the dates above.")

        # ---- what: a saved set of channels
        box = row(4, "Batch")
        self.batch_var = tk.StringVar()
        self.batch_box = ttk.Combobox(box, textvariable=self.batch_var, width=28,
                                      state="readonly", values=[])
        self.batch_box.pack(side="left")
        self.batch_box.bind("<<ComboboxSelected>>", self._on_batch_selected)
        Tip(self.batch_box, "A batch is a saved set of channels. Picking one fills in "
                            "the end date with the day it was last queued.")
        self.batch_info = tk.StringVar(value="no batches saved yet")
        ttk.Label(box, textvariable=self.batch_info, foreground=GREY).pack(
            side="left", padx=10)
        for txt, cmd in (("Delete", self._delete_batch),
                         ("Edit...", self._edit_batch),
                         ("New...", lambda: self._edit_batch(new=True))):
            ttk.Button(box, text=txt, command=cmd, width=7).pack(side="right", padx=2)
        action(4, "Queue batch", self._queue_batch,
               "Queue one job for every channel in this batch, for the dates above.")

        # ---- what: one person's messages
        box = row(5, "Username")
        self.user_var = tk.StringVar()
        entry = ttk.Entry(box, textvariable=self.user_var, width=22)
        entry.pack(side="left")
        Tip(entry, "Their exact username (the @name on their profile), "
                   "not their display name.")
        ttk.Label(box, text="Server").pack(side="left", padx=(10, 4))
        self.server_var = tk.StringVar()
        self.server_box = ttk.Combobox(box, textvariable=self.server_var, width=26,
                                       state="readonly", values=[])
        self.server_box.pack(side="left")
        Tip(self.server_box, "The servers this app has already visited.")
        ttk.Label(box, text="or a channel link").pack(side="left", padx=(10, 4))
        self.user_link_var = tk.StringVar()
        entry = ttk.Entry(box, textvariable=self.user_link_var)
        entry.pack(side="left", fill="x", expand=True)
        Tip(entry, "For a server that is not in the list yet: a link to any "
                   "channel of that server.")
        action(5, "Add user", self._add_user_job,
               "Queue everything this person posted in the server for the dates "
               "above, each message with the conversation around it.\n"
               "Tip: scrape the busy channels for those dates first; it then "
               "goes much quicker.")

    def _build_lower(self, outer):
        # ---- queue table ----
        qf = ttk.LabelFrame(outer, text="Queue")
        qf.grid(row=1, column=0, sticky="nsew", padx=8, pady=2)
        qf.columnconfigure(0, weight=1)
        qf.rowconfigure(0, weight=1)

        cols = ("channel", "from", "to", "output", "status")
        self.tree = ttk.Treeview(qf, columns=cols, show="headings", height=5,
                                 selectmode="extended")
        for c, w, t in (("channel", 180, "Job"), ("from", 80, "From"),
                        ("to", 80, "To"), ("output", 280, "Output file"),
                        ("status", 180, "Status")):
            self.tree.heading(c, text=t)
            # The two date columns never need more room; the rest share it.
            self.tree.column(c, width=w, anchor="w", stretch=c not in ("from", "to"))
        self.tree.grid(row=0, column=0, sticky="nsew")
        tsb = ttk.Scrollbar(qf, command=self.tree.yview)
        tsb.grid(row=0, column=1, sticky="ns")
        self.tree.config(yscrollcommand=tsb.set)
        self.tree.tag_configure(RUNNING, foreground="#0a58ca")
        self.tree.tag_configure(DONE, foreground="#1a7f37")
        self.tree.tag_configure(FAILED, foreground="#b42318")
        self.tree.tag_configure(STOPPED, foreground="#96700a")
        self.tree.tag_configure(INCOMPLETE, foreground="#b45309")

        qbtn = ttk.Frame(qf)
        qbtn.grid(row=1, column=0, columnspan=2, sticky="w", padx=4, pady=3)
        for txt, cmd, tip in (
                ("Move up", lambda: self._move(-1), ""),
                ("Move down", lambda: self._move(1), ""),
                ("Remove selected", self._remove_selected, ""),
                ("Clear finished", self._clear_finished,
                 "Removes done and failed jobs. Stopped and incomplete jobs stay: "
                 "they resume when you press Start queue."),
                ("Run again", self._run_again,
                 "Runs the selected jobs again. Only what the archive doesn't "
                 "hold yet is collected."),
                ("Start over", self._start_over,
                 "Scrolls through the selected jobs' whole date range again."),
                ("Save as batch...", self._batch_from_queue,
                 "Makes a new batch from the channels in the queue.")):
            btn = ttk.Button(qbtn, text=txt, command=cmd)
            btn.pack(side="left", padx=3)
            if tip:
                Tip(btn, tip)

        # ---- options + controls ----
        s = self.settings
        opt = ttk.Frame(outer)
        opt.grid(row=2, column=0, sticky="w", padx=10, pady=2)
        self.awake_var = tk.BooleanVar(value=s.get("keep_awake", True))
        tick = ttk.Checkbutton(opt, text="Keep PC awake", variable=self.awake_var)
        tick.pack(side="left")
        Tip(tick, "No sleep and no screensaver while the queue runs.")
        self.saveimg_var = tk.BooleanVar(value=s.get("save_images", True))
        tick = ttk.Checkbutton(opt, text="Save pictures", variable=self.saveimg_var,
                               command=self._sync_picture_boxes)
        tick.pack(side="left", padx=(12, 0))
        Tip(tick, "Save the pictures posted in messages (attachments and link "
                  "previews). Each one is listed next to its message in the "
                  "output file.")
        self.fullimg_var = tk.BooleanVar(value=s.get("full_images", True))
        self.fullimg_box = ttk.Checkbutton(opt, text="Full-size",
                                           variable=self.fullimg_var)
        self.fullimg_box.pack(side="left", padx=(12, 0))
        Tip(self.fullimg_box, "Also fetch full-size copies when a job finishes. "
                              "Slower; without it you keep the smaller previews.")
        ttk.Label(opt, text="Per-job limit").pack(side="left", padx=(14, 4))
        self.budget_var = tk.StringVar(value=s.get("budget", ""))
        entry = ttk.Entry(opt, textvariable=self.budget_var, width=5)
        entry.pack(side="left")
        Tip(entry, "Stop each job after this many minutes. Blank = no limit. A job "
                   "that runs out of time carries on next time.")
        ttk.Label(opt, text="min").pack(side="left", padx=4)
        self._sync_picture_boxes()

        ctl = ttk.Frame(outer)
        ctl.grid(row=2, column=0, sticky="e", padx=8, pady=2)
        self.start_btn = ttk.Button(ctl, text="Start queue", command=self._start_queue)
        self.start_btn.pack(side="left", padx=3)
        self.pause_btn = ttk.Button(ctl, text="Pause", command=self._toggle_pause,
                                    state="disabled", width=9)
        self.pause_btn.pack(side="left", padx=3)
        self.finish_btn = ttk.Button(ctl, text="Stop after this job",
                                     command=self._stop_after, state="disabled")
        self.finish_btn.pack(side="left", padx=3)
        self.stop_btn = ttk.Button(ctl, text="Stop now", command=self._stop_now,
                                   state="disabled")
        self.stop_btn.pack(side="left", padx=3)

        # ---- log ----
        logf = ttk.LabelFrame(outer, text="Progress")
        logf.grid(row=3, column=0, sticky="nsew", padx=8, pady=2)
        logf.rowconfigure(0, weight=1)
        logf.columnconfigure(0, weight=1)
        self.log_txt = tk.Text(logf, height=7, wrap="word", state="disabled",
                               bg="#1e1e1e", fg="#d4d4d4", insertbackground="#d4d4d4")
        self.log_txt.grid(row=0, column=0, sticky="nsew")
        lsb = ttk.Scrollbar(logf, command=self.log_txt.yview)
        lsb.grid(row=0, column=1, sticky="ns")
        self.log_txt.config(yscrollcommand=lsb.set)

        status = ttk.Frame(outer)
        status.grid(row=4, column=0, sticky="ew", padx=10, pady=(0, 4))
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(status, textvariable=self.status_var).pack(side="left")

        self._log("Add jobs to the queue, then press Start queue.")
        self._log("Nothing is scraped twice: a job only collects what the archive doesn't "
                  "hold yet, so a stopped job carries on where it left off.")

    def _sync_picture_boxes(self):
        """'Full-size' only means something while pictures are being saved."""
        self.fullimg_box.config(
            state="normal" if self.saveimg_var.get() else "disabled")

    # ---------------- form helpers ----------------
    def _browse_dir(self):
        d = filedialog.askdirectory(title="Folder to save scrapes in",
                                    initialdir=self.out_var.get() or str(Path.home()))
        if d:
            self.out_var.set(d)

    def _browse_file(self):
        draft = {"type": CHANNEL_JOB,
                 "label": self.label_var.get().strip() or "channel",
                 "start": self.start_var.get().strip(),
                 "end": self.end_var.get().strip()}
        p = filedialog.asksaveasfilename(
            title="Save this job's messages as", defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
            initialdir=self.out_var.get() or str(Path.home()),
            initialfile=jobs.file_name(draft))
        if p:
            self.out_var.set(p)

    def _clear_form(self):
        for var in (self.start_var, self.end_var, self.link_var, self.label_var,
                    self.user_var, self.user_link_var):
            var.set("")

    def _parse_date(self, text, field, end_of_day):
        text = text.strip()
        if not text:
            return None
        try:
            d = datetime.strptime(text, "%Y-%m-%d")
        except ValueError:
            raise ValueError(f"{field} must be YYYY-MM-DD (got '{text}').")
        if end_of_day:
            d = d.replace(hour=23, minute=59, second=59)
        return d.replace(tzinfo=timezone.utc)

    def _check_dates(self, start_raw, end_raw):
        """The two date boxes, stripped, after checking they make sense."""
        start_raw, end_raw = start_raw.strip(), end_raw.strip()
        start_dt = self._parse_date(start_raw, "Start date", True)
        end_dt = self._parse_date(end_raw, "End date", False)
        if start_dt and end_dt and end_dt >= start_dt:
            raise ValueError("End date must be older than start date "
                             "(scraping runs backwards in time).")
        if end_dt and end_dt > datetime.now(timezone.utc):
            raise ValueError("End date is in the future.")
        return start_raw, end_raw

    def _destination(self):
        """(folder, exact .json path or None) from the 'Save to' box."""
        dest = self.out_var.get().strip()
        if not dest:
            raise ValueError("Choose a folder (or file) to save into.")
        dest = Path(dest)
        explicit = dest if dest.suffix.lower() == ".json" else None
        folder = dest.parent if explicit else dest
        if not folder.exists():
            raise ValueError(f"Folder does not exist: {folder}")
        return folder, explicit

    def _check_free(self, out, also=()):
        if any(j["out"] == out and j["status"] in (PENDING, RUNNING)
               for j in list(self.jobs) + list(also)):
            raise ValueError(f"A pending job already writes to {Path(out).name}.")

    def _enqueue(self, new, folder):
        self.jobs.extend(new)
        self.settings["last_out_dir"] = str(folder)
        self._save_settings()
        self._refresh_servers()
        self._refresh_tree()
        self._save_queue()
        for j in new:
            self._log(f"Queued: {j['label']} -> {Path(j['out']).name}")

    def _add_job(self):
        try:
            guild_id, channel_id = parse_channel_link(self.link_var.get())
            start, end = self._check_dates(self.start_var.get(), self.end_var.get())
            folder, explicit = self._destination()
            label, auto = self._label_for(guild_id, channel_id, self.label_var.get())
            job = jobs.new_job(CHANNEL_JOB, label=label, auto_label=auto,
                               guild_id=guild_id, channel_id=channel_id,
                               start=start, end=end, auto_out=explicit is None)
            job["out"] = str(explicit or folder / jobs.file_name(job))
            self._check_free(job["out"])
        except ValueError as e:
            messagebox.showerror("Check the job", str(e))
            return
        self._enqueue([job], folder)
        self.link_var.set("")      # the dates stay, ready for the next channel
        self.label_var.set("")

    def _add_user_job(self):
        try:
            name = self.user_var.get().strip().lstrip("@").strip()
            if not name:
                raise ValueError("Enter a username.")
            if any(c.isspace() for c in name):
                raise ValueError("A username has no spaces. Use the @name shown on "
                                 "their profile, not their display name.")
            start, end = self._check_dates(self.start_var.get(), self.end_var.get())
            folder, explicit = self._destination()
            link = self.user_link_var.get().strip()
            pick = self.server_var.get()
            if link:
                targets = [parse_channel_link(link)]
            elif pick == ALL_SERVERS:
                targets = list(self.server_map.values())
            elif pick in self.server_map:
                targets = [self.server_map[pick]]
            else:
                raise ValueError("Pick a server, or paste a link to any channel "
                                 "of the server.")
            if explicit and len(targets) > 1:
                raise ValueError("Several servers make several files: choose a "
                                 "folder in 'Save to', not a .json file.")
            new = []
            for guild_id, channel_id in targets:
                server, auto = self._server_label(guild_id)
                job = jobs.new_job(USER_JOB, label=f"{name} @ {server}",
                                   auto_label=auto, guild_id=guild_id,
                                   channel_id=channel_id, start=start, end=end,
                                   auto_out=explicit is None, username=name,
                                   server=server)
                job["out"] = str(explicit or folder / jobs.file_name(job))
                self._check_free(job["out"], new)
                new.append(job)
        except ValueError as e:
            messagebox.showerror("Check the job", str(e))
            return
        self._enqueue(new, folder)
        self.settings["last_server"] = pick
        self._save_settings()
        self.user_var.set("")
        self.user_link_var.set("")

    # ---------------- names ----------------
    def _batch_label(self, channel_id):
        """A real (hand-typed) label some batch already gives this channel."""
        for b in list(self.batches):   # a copy: the worker thread calls this too
            for ch in list(b.get("channels", [])):
                if (ch.get("channel_id") == channel_id
                        and not jobs.is_placeholder(ch.get("label"))):
                    return ch["label"]
        return None

    def _label_for(self, guild_id, channel_id, typed=""):
        """(label, is it only a stand-in?) for a channel."""
        typed = (typed or "").strip()
        if not jobs.is_placeholder(typed):
            return typed, False
        label = (self._batch_label(channel_id)
                 or jobs.real_label(self.store, guild_id, channel_id))
        if label:
            return label, False
        return jobs.placeholder(channel_id), True

    def _server_label(self, guild_id):
        """(short server name, is it only a guess?)."""
        name = jobs.short_server(self.store.name(guild_id))
        if name:
            return name, False
        labels, whole = [], []
        for b in list(self.batches):
            chans = list(b.get("channels", []))
            mine =[c for c in chans if c.get("guild_id") == guild_id]
            labels += [c.get("label") for c in mine]
            if mine and len(mine) == len(chans):
                whole.append(b["name"])
        guess = jobs.guess_server(labels) or (min(whole, key=len) if whole else None)
        return guess or f"server {str(guild_id)[-4:]}", True

    def _relabel(self, job):
        """Swap a stand-in label for the real name once it is known. It only
        changes values, never keys, so the worker thread may call it."""
        if not job.get("auto_label"):
            return
        if job["type"] == USER_JOB:
            server, auto = self._server_label(job["guild_id"])
            if not auto:
                job["server"] = server
                job["label"] = f"{job['username']} @ {server}"
                job["auto_label"] = False
        else:
            label, auto = self._label_for(job["guild_id"], job["channel_id"])
            if not auto:
                job["label"] = label
                job["auto_label"] = False

    def _sync_out(self, job):
        """Rename an auto-named output file after its job's label changed.
        Never overwrites anything: if the new name is taken, the old one stays."""
        if not job.get("auto_out"):
            return
        old = Path(job["out"])
        new = old.with_name(jobs.file_name(job))
        if new == old or any(j is not job and j["out"] == str(new)
                             for j in list(self.jobs)):
            return
        if old.exists() and new.exists():
            return
        try:
            if old.exists():
                os.replace(old, new)
                md_old, md_new = old.with_suffix(".md"), new.with_suffix(".md")
                if job["type"] == USER_JOB and md_old.exists() and not md_new.exists():
                    os.replace(md_old, md_new)
        except OSError:
            return
        job["out"] = str(new)

    def _apply_known_names(self, sync_files=False):
        """Put real channel and server names wherever a stand-in is still
        showing: batch labels, queued jobs, the server list. Main thread only.

        Files are only renamed when sync_files is set, which is never while a
        queue is running: the worker renames its own job's file itself.
        """
        changed = False
        for b in self.batches:
            for ch in b.get("channels", []):
                if jobs.is_placeholder(ch.get("label")):
                    label, auto = self._label_for(ch.get("guild_id"),
                                                  ch.get("channel_id"))
                    if not auto:
                        ch["label"] = label
                        changed = True
        if changed:
            self._save_batches()
        for j in self.jobs:
            self._relabel(j)
            if sync_files and j["status"] != RUNNING:
                self._sync_out(j)
        self._refresh_servers()
        self._refresh_tree()
        self._save_queue()

    def _refresh_servers(self):
        """Fill the server dropdown on the Username row from every server the
        app has heard of (batches, the queue, the archive)."""
        anchors = {}
        for b in self.batches:
            for ch in b.get("channels", []):
                if ch.get("guild_id") and ch.get("channel_id"):
                    anchors.setdefault(ch["guild_id"], ch["channel_id"])
        for j in self.jobs:
            anchors.setdefault(j["guild_id"], j["channel_id"])
        for gid in self.store.guild_ids():
            if gid in anchors:
                continue
            ch = self.store.main_channel(gid)
            if not ch:
                chans = self.store.channels_of(gid)
                ch = chans[0][0] if chans else None
            if ch:
                anchors[gid] = ch
        self.server_map = {}
        for gid, ch in anchors.items():
            name = self._server_label(gid)[0]
            if name in self.server_map:
                name = f"{name} ({str(gid)[-4:]})"
            self.server_map[name] = (gid, ch)
        values = sorted(self.server_map, key=str.lower)
        if len(values) > 1:
            values.append(ALL_SERVERS)
        self.server_box.config(values=values)
        want = self.server_var.get() or self.settings.get("last_server", "")
        if want not in values:
            want = values[0] if values else ""
        self.server_var.set(want)

    # ---------------- batches ----------------
    def _refresh_batches(self, select=None):
        """Repopulate the batch dropdown, optionally selecting one by name."""
        was = self.batch_var.get()
        names = [b["name"] for b in self.batches]
        self.batch_box.config(values=names)
        if select and select in names:
            self.batch_var.set(select)
        elif self.batch_var.get() not in names:
            self.batch_var.set(names[0] if names else "")
        # Setting the variable does NOT fire <<ComboboxSelected>>, so do the
        # same prefill by hand -- otherwise a batch restored at startup shows
        # in the box with an empty end date and would scrape from the beginning.
        # Only when the selection really changed: the date boxes are shared
        # with the other kinds of job, so they are not to be overwritten idly.
        if self.batch_var.get() != was:
            self._prefill_batch_dates()
        self._refresh_batch_info()

    def _current_batch(self):
        name = self.batch_var.get()
        return next((b for b in self.batches if b["name"] == name), None)

    def _refresh_batch_info(self):
        b = self._current_batch()
        if not b:
            self.batch_info.set("no batches yet — 'New...' makes one")
            return
        last = b.get("last_queued")
        self.batch_info.set(f"{len(b.get('channels', []))} channel(s)" +
                            (f" · last queued {last}" if last else " · never queued"))

    def _on_batch_selected(self, _event=None):
        self._prefill_batch_dates()
        self._refresh_batch_info()

    def _prefill_batch_dates(self):
        """Fill the date boxes from the selected batch's own history."""
        b = self._current_batch()
        if not b:
            return
        # A top-up run normally wants "everything since the last one", so the
        # end (oldest) date defaults to the day this batch was last queued.
        self.start_var.set("")
        self.end_var.set(b.get("last_queued", ""))
        out_dir = b.get("out_dir", "")
        if out_dir and Path(out_dir).exists():
            self.out_var.set(out_dir)
        self._log(f"Batch '{b['name']}': {len(b.get('channels', []))} channel(s)"
                  + (f", back to {b['last_queued']}" if b.get("last_queued") else ""))

    def _edit_batch(self, new=False):
        if new:
            batch = {"name": "", "channels": []}
        else:
            batch = self._current_batch()
            if not batch:
                messagebox.showinfo("Pick a batch",
                                    "Select a batch to edit, or press 'New...'.")
                return
        self._open_editor(batch)

    def _batch_from_queue(self):
        """Seed a new batch with every distinct channel currently in the queue."""
        seen, channels = set(), []
        for j in self.jobs:
            if j.get("type") == USER_JOB:
                continue
            key = (j["guild_id"], j["channel_id"])
            if key in seen:
                continue
            seen.add(key)
            channels.append({"label": j["label"], "guild_id": j["guild_id"],
                             "channel_id": j["channel_id"]})
        if not channels:
            messagebox.showinfo("Empty queue",
                                "The queue has no jobs to take channels from.")
            return
        self._open_editor({"name": "", "channels": channels})

    def _open_editor(self, batch):
        original = batch.get("name", "")
        edited = BatchEditor(self.root, batch,
                             [b["name"] for b in self.batches],
                             namer=lambda g, c: self._label_for(g, c)[0]).result
        if edited is None:
            return
        existing = next((b for b in self.batches if b["name"] == original), None)
        if existing:
            existing["name"] = edited["name"]
            existing["channels"] = edited["channels"]
        else:
            self.batches.append(edited)
        self.batches.sort(key=lambda b: b["name"].lower())
        self._save_batches()
        self._refresh_batches(select=edited["name"])
        self._log(f"Saved batch '{edited['name']}' "
                  f"({len(edited['channels'])} channel(s)).")

    def _delete_batch(self):
        b = self._current_batch()
        if not b:
            return
        if not messagebox.askyesno("Delete batch",
                                   f"Delete the batch '{b['name']}'?\n\n"
                                   "Scrapes already collected are not touched."):
            return
        self.batches = [x for x in self.batches if x["name"] != b["name"]]
        self._save_batches()
        self._refresh_batches()
        self._log(f"Deleted batch '{b['name']}'.")

    def _batch_folder(self):
        """Return (base folder, run folder) for a batch run.

        The run folder is a dated subfolder when that option is ticked; the
        base is what gets remembered on the batch, so the next run dates itself
        afresh instead of nesting inside the last run's folder.
        """
        dest = self.out_var.get().strip()
        if not dest:
            raise ValueError("Choose a folder to save into (the 'Save to' box).")
        base = Path(dest)
        if base.suffix.lower() == ".json":
            base = base.parent   # a batch needs a folder: use that file's folder
        if not base.exists():
            raise ValueError(f"Folder does not exist: {base}")
        if not self.dated_var.get():
            return base, base
        run = base / datetime.now().strftime("%m-%d")
        try:
            run.mkdir(exist_ok=True)
        except OSError as e:
            raise ValueError(f"Could not create {run}: {e}")
        return base, run

    def _queue_batch(self):
        batch = self._current_batch()
        if not batch:
            messagebox.showinfo("Pick a batch", "Select a batch first.")
            return
        if not batch.get("channels"):
            messagebox.showerror("Empty batch",
                                 f"'{batch['name']}' has no channels in it. "
                                 "Press 'Edit...' to add some.")
            return
        try:
            start_raw, end_raw = self._check_dates(self.start_var.get(),
                                                   self.end_var.get())
            base, folder = self._batch_folder()
        except ValueError as e:
            messagebox.showerror("Check the batch", str(e))
            return
        # The date boxes are shared with single jobs, where a blank end date is
        # common. Across a whole batch it is a very long run, so make sure.
        if not end_raw and not messagebox.askyesno(
                "No end date",
                f"The end date is blank, so all {len(batch['channels'])} channel(s) "
                f"in '{batch['name']}' would be collected back to their very "
                "beginning.\n\nQueue them like that?"):
            return

        added, skipped = 0, []
        for ch in batch["channels"]:
            label, auto = self._label_for(ch["guild_id"], ch["channel_id"],
                                          ch.get("label"))
            job = jobs.new_job(CHANNEL_JOB, label=label, auto_label=auto,
                               guild_id=ch["guild_id"], channel_id=ch["channel_id"],
                               start=start_raw, end=end_raw)
            job["out"] = str(folder / jobs.file_name(job))
            clash = next((j for j in self.jobs if j["out"] == job["out"]
                          and j["status"] in (PENDING, RUNNING)), None)
            if clash and clash["channel_id"] == job["channel_id"]:
                skipped.append(label)
                continue
            if clash:   # two channels with the same label: tell the files apart
                job["label"] = f"{label} {ch['channel_id'][-4:]}"
                job["auto_label"] = False
                job["out"] = str(folder / jobs.file_name(job))
            self.jobs.append(job)
            added += 1

        batch["last_queued"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        batch["out_dir"] = str(base)
        self._save_batches()
        self.settings["last_out_dir"] = str(base)
        self.settings["batch_dated_dir"] = self.dated_var.get()
        self._save_settings()
        self._refresh_tree()
        self._save_queue()
        self._refresh_batch_info()
        self._log(f"Queued batch '{batch['name']}': {added} job(s), "
                  f"{end_raw or 'the beginning'} to {start_raw or 'now'}, "
                  f"into {folder}")
        if skipped:
            self._log("Already pending, not queued again: " + ", ".join(skipped))

    # ---------------- queue table ----------------
    def _refresh_tree(self):
        sel = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        for i, j in enumerate(self.jobs):
            status = j["status"] + (f" — {j['note']}" if j.get("note") else "")
            iid = str(i)
            self.tree.insert("", "end", iid=iid, values=(
                j["label"], j["start"] or "now", j["end"] or "beginning",
                Path(j["out"]).name, status), tags=(j["status"],))
            if iid in sel:
                self.tree.selection_add(iid)

    def _selected_indexes(self):
        return sorted(int(i) for i in self.tree.selection())

    def _busy(self):
        return self.worker is not None and self.worker.is_alive()

    def _move(self, delta):
        idxs = self._selected_indexes()
        if not idxs:
            return
        order = idxs if delta < 0 else list(reversed(idxs))
        moved = []
        for i in order:
            n = i + delta
            if 0 <= n < len(self.jobs) and self.jobs[i]["status"] != RUNNING:
                self.jobs[i], self.jobs[n] = self.jobs[n], self.jobs[i]
                moved.append(n)
        self._refresh_tree()
        self.tree.selection_set([str(i) for i in moved])
        self._save_queue()

    def _remove_selected(self):
        idxs = self._selected_indexes()
        kept = [j for i, j in enumerate(self.jobs)
                if i not in idxs or j["status"] == RUNNING]
        removed = len(self.jobs) - len(kept)
        self.jobs = kept
        self._refresh_tree()
        self._save_queue()
        if removed:
            self._log(f"Removed {removed} job(s) from the queue.")

    def _clear_finished(self):
        before = len(self.jobs)
        # Stopped and incomplete jobs stay: they still have history to collect.
        self.jobs = [j for j in self.jobs
                     if j["status"] in (PENDING, RUNNING, INCOMPLETE, STOPPED)]
        self._refresh_tree()
        self._save_queue()
        self._log(f"Cleared {before - len(self.jobs)} finished job(s).")

    def _run_again(self):
        """Selected jobs back to pending. A job only collects what the archive
        lacks, so running a finished one again costs little."""
        idxs = [i for i in self._selected_indexes()
                if self.jobs[i]["status"] != RUNNING]
        if not idxs:
            messagebox.showinfo("Pick a job", "Select one or more jobs in the queue first.")
            return
        for i in idxs:
            self.jobs[i]["status"], self.jobs[i]["note"] = PENDING, ""
        self._refresh_tree()
        self._save_queue()
        self._log(f"{len(idxs)} job(s) set to run again. Only what the archive "
                  "doesn't hold yet is collected.")

    def _start_over(self):
        """Selected jobs back to pending, and their range scrolled in full."""
        idxs = [i for i in self._selected_indexes()
                if self.jobs[i]["status"] != RUNNING]
        if not idxs:
            messagebox.showinfo("Pick a job", "Select one or more jobs in the queue first.")
            return
        if not messagebox.askyesno(
                "Start over",
                f"Scroll through the whole date range again for {len(idxs)} job(s)?\n\n"
                "Nothing is deleted. Use this to pick up edits made since the last "
                "scrape, or pictures that were not saved the first time.\n\n"
                "It takes as long as the first scrape did."):
            return
        for i in idxs:
            j = self.jobs[i]
            j["status"], j["note"], j["fresh"] = PENDING, "", True
        self._refresh_tree()
        self._save_queue()
        self._log(f"{len(idxs)} job(s) set to start over.")

    # ---------------- logging ----------------
    def _log(self, msg):
        self.log_q.put(msg)
        try:  # also to disk, so an unattended run can be diagnosed afterwards
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
        except Exception:
            pass

    def _drain_log(self):
        while True:
            try:
                msg = self.log_q.get_nowait()
            except queue.Empty:
                break
            self.log_txt.config(state="normal")
            self.log_txt.insert("end", f"[{datetime.now():%H:%M:%S}] {msg}\n")
            self.log_txt.see("end")
            self.log_txt.config(state="disabled")
        while True:
            try:
                fn = self.ui_q.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception as e:   # one bad update must not stop the loop
                self.log_q.put(f"(display update failed: {type(e).__name__}: {e})")
        self.root.after(150, self._drain_log)

    def _ui(self, fn):
        """Schedule a UI update from the worker thread.

        Tkinter is not thread-safe, so the worker never touches widgets: it
        queues a callable that the main thread runs from _drain_log.
        """
        self.ui_q.put(fn)

    # ---------------- running ----------------
    def _capture_options(self):
        """Everything the worker needs from the option boxes, as plain values.

        Tk variables may only be touched on the main thread, so they are all
        read here, before the worker starts. Raises ValueError with a message
        fit for an error box.
        """
        budget = self.budget_var.get().strip()
        try:
            max_minutes = int(budget) if budget else None
            if max_minutes is not None and max_minutes <= 0:
                raise ValueError
        except ValueError:
            raise ValueError("Per-job limit must be a whole number of minutes (or blank).")
        opts = {"keep_awake": self.awake_var.get(), "max_minutes": max_minutes,
                "save_images": self.saveimg_var.get(),
                "full_images": self.fullimg_var.get()}
        self.settings.update(keep_awake=opts["keep_awake"], budget=budget,
                             save_images=opts["save_images"],
                             full_images=opts["full_images"])
        self._save_settings()
        return opts

    def _launch(self, target):
        self.stop_current.clear()
        self.stop_queue.clear()
        self.pause_event.clear()
        self.start_btn.config(state="disabled")
        self.pause_btn.config(state="normal", text="Pause")
        self.stop_btn.config(state="normal")
        self.finish_btn.config(state="normal")
        self.worker = threading.Thread(target=target, daemon=True)
        self.worker.start()

    def _start_queue(self):
        if self._busy():
            return
        if not any(j["status"] in (PENDING, STOPPED, INCOMPLETE) for j in self.jobs):
            messagebox.showinfo("Nothing to do",
                                "No jobs are waiting. Add one, or select a finished "
                                "job and press 'Run again'.")
            return
        try:
            opts = self._capture_options()
        except ValueError as e:
            messagebox.showerror("Check the options", str(e))
            return

        again = [j for j in self.jobs if j["status"] in (STOPPED, INCOMPLETE)]
        for j in again:
            j["status"], j["note"] = PENDING, ""
        if again:
            self._log(f"Resuming {len(again)} unfinished job(s): they carry on "
                      "from where they left off.")
        pics = "not saved"
        if opts["save_images"]:
            pics = "saved" + (", with full-size copies" if opts["full_images"]
                              else " (previews only)")
        self._log(f"Pictures: {pics}.")
        self._refresh_tree()
        self._save_queue()
        self._launch(lambda: self._run_queue(opts))

    def _halted(self):
        return self.stop_queue.is_set() or self.stop_current.is_set()

    def _run_queue(self, opts):
        if opts["keep_awake"]:
            if keep_awake.prevent_idle():
                self._log("Sleep and screensaver disabled for this queue run.")
            else:
                self._log("NOTE: could not disable sleep/screensaver on this system.")
        totals = {DONE: 0, FAILED: 0, STOPPED: 0, INCOMPLETE: 0, "messages": 0, "new": 0}
        try:
            while True:
                job = next((j for j in self.jobs if j["status"] == PENDING), None)
                if job is None or self._halted():
                    break
                job["status"], job["note"] = RUNNING, ""
                self._relabel(job)
                self._sync_out(job)
                pend = sum(1 for j in self.jobs if j["status"] == PENDING)
                self._ui(self._refresh_tree)
                self._ui(lambda l=job["label"], p=pend: self.status_var.set(
                    f"Running: {l}   ({p} more waiting)"))
                self._log("")
                self._log(f"===== {job['label']}: {job['end'] or 'the beginning'} "
                          f"to {job['start'] or 'now'} =====")
                try:
                    self._run_job(job, opts, totals)
                except ValueError as e:
                    job["status"], job["note"] = FAILED, str(e)
                    totals[FAILED] += 1
                    self._log(f"ERROR: {e}")
                except Exception as e:
                    job["status"], job["note"] = FAILED, type(e).__name__
                    totals[FAILED] += 1
                    self._log(f"ERROR: {type(e).__name__}: {e}")
                    self._log("Continuing with the next job.")
                self._ui(self._apply_known_names)

                if any(j["status"] == PENDING for j in self.jobs) and not self._halted():
                    wait = random.uniform(JOB_PAUSE_MIN_S, JOB_PAUSE_MAX_S)
                    self._log(f"Next job in {wait:.0f}s...")
                    self._ui(lambda: self.status_var.set(
                        "Short break before the next job..."))
                    waited = 0.0
                    while waited < wait and not self._halted():
                        time.sleep(0.5)
                        if not self.pause_event.is_set():   # a pause stops the clock
                            waited += 0.5
        finally:
            if opts["keep_awake"]:
                keep_awake.clear()
                self._log("Sleep and screensaver settings restored.")
            self._log("")
            self._log(f"Queue finished: {totals[DONE]} done, {totals[INCOMPLETE]} "
                      f"incomplete, {totals[FAILED]} failed, {totals[STOPPED]} stopped. "
                      f"{totals['messages']} messages in the files, "
                      f"{totals['new']} newly collected.")
            if totals[INCOMPLETE] or totals[STOPPED]:
                self._log("Unfinished jobs carry on from where they left off the "
                          "next time you press Start queue.")
            left = sum(1 for j in self.jobs if j["status"] == PENDING)
            if left:
                self._log(f"{left} job(s) still waiting.")
            self._ui(lambda: self._apply_known_names(sync_files=True))
            self._ui(lambda: self.status_var.set("Ready."))
            self._ui(self._reset_buttons)

    def _run_job(self, job, opts, totals):
        start_dt = self._parse_date(job["start"], "Start date", True)
        end_dt = self._parse_date(job["end"], "End date", False)
        fresh, job["fresh"] = bool(job["fresh"]), False
        common = dict(
            guild_id=job["guild_id"], channel_id=job["channel_id"],
            start_dt=start_dt, end_dt=end_dt, out_path=Path(job["out"]),
            profile_dir=PROFILE_DIR, log=self._log, stop_event=self.stop_current,
            store=self.store, max_minutes=opts["max_minutes"],
            pause_event=self.pause_event, fresh=fresh,
            images_dir=IMAGES_DIR if opts["save_images"] else None,
            full_images=opts["full_images"])
        if job["type"] == USER_JOB:
            res = asyncio.run(user_history.run_user_history(
                username=job["username"], **common))
        else:
            res = asyncio.run(run_scrape(**common))

        status, note = jobs.outcome_status(job, res)
        job["status"], job["note"] = status, note
        totals[status] += 1
        totals["messages"] += res.get("count", 0)
        totals["new"] += res.get("new", 0)
        if status == INCOMPLETE:
            self._log(f"NOT finished: {note}. Press Start queue again later and "
                      "it carries on from where it stopped.")
        elif status == FAILED:
            self._log(f"ERROR: {note}")
        # The job may have just learned the channel's real name.
        self._relabel(job)
        self._sync_out(job)

    def _reset_buttons(self):
        self.pause_event.clear()
        self.start_btn.config(state="normal")
        self.pause_btn.config(state="disabled", text="Pause")
        self.stop_btn.config(state="disabled")
        self.finish_btn.config(state="disabled")

    def _toggle_pause(self):
        if not self._busy():
            return
        if self.pause_event.is_set():
            self.pause_event.clear()
            self.pause_btn.config(text="Pause")
            self.status_var.set("Running.")
        else:
            self.pause_event.set()
            self.pause_btn.config(text="Resume")
            self.status_var.set("Paused. Press Resume to carry on.")

    def _stop_after(self):
        self.stop_queue.set()
        self.finish_btn.config(state="disabled")
        self._log("Will stop after the current job finishes.")
        self.status_var.set("Finishing current job, then stopping...")

    def _stop_now(self):
        self.stop_queue.set()
        self.stop_current.set()
        self.pause_event.clear()
        self.pause_btn.config(state="disabled", text="Pause")
        self.stop_btn.config(state="disabled")
        self.finish_btn.config(state="disabled")
        self._log("Stopping now. Everything captured so far is kept.")
        self.status_var.set("Stopping...")

    # ---------------- tools ----------------
    def _archive_summary(self):
        s = self.store.stats()
        try:
            size = ARCHIVE_DB.stat().st_size / 1_048_576
        except OSError:
            size = 0.0
        messagebox.showinfo(
            "Archive",
            f"{s['messages']:,} messages from {s['channels']} channel(s) in "
            f"{len(self.store.guild_ids())} server(s).\n"
            f"{s['images']:,} pictures seen, {s['images_saved']:,} saved.\n\n"
            f"Archive: {ARCHIVE_DB} ({size:.1f} MB)\nPictures: {IMAGES_DIR}")

    def _open_path(self, path, make=False):
        try:
            if make:
                Path(path).mkdir(parents=True, exist_ok=True)
            os.startfile(str(path))
        except (OSError, AttributeError) as e:
            messagebox.showerror("Could not open", f"{path}\n\n{e}")

    def _log_out(self):
        """Forget the Discord login saved in the app's browser.

        The login lives in the browser's profile folder, so logging out means
        removing that folder. It is renamed first: Windows refuses to rename a
        folder while the browser still has a file in it open, so either the
        whole login goes or nothing is touched."""
        if self._busy():
            messagebox.showinfo(
                "Log out of Discord",
                "The queue is running, and the browser is using the login.\n\n"
                "Stop the queue first, then log out.")
            return
        try:
            saved = PROFILE_DIR.is_dir() and any(PROFILE_DIR.iterdir())
        except OSError:
            saved = True
        if not saved:
            messagebox.showinfo("Log out of Discord",
                                "No Discord login is saved on this PC.")
            return
        if not messagebox.askyesno(
                "Log out of Discord",
                "Forget the Discord login saved on this PC?\n\n"
                "The next job opens Discord's login page, where you can log in "
                "with the same account or a different one.\n\n"
                "Everything you collected, your batches and your queue are kept.",
                default="no"):
            return
        old = PROFILE_DIR.with_name(PROFILE_DIR.name + ".removing")
        try:
            if old.exists():
                shutil.rmtree(old)
            PROFILE_DIR.rename(old)
        except OSError as e:
            messagebox.showerror(
                "Log out of Discord",
                "Could not remove the saved login. Nothing was changed.\n\n"
                f"If a {APP_NAME} browser window is still open, close it and "
                f"try again.\n\n{e}")
            return
        shutil.rmtree(old, ignore_errors=True)
        self._log("Logged out of Discord: the saved login was removed from this PC.")
        messagebox.showinfo(
            "Log out of Discord",
            "Done. The next job will ask you to log in to Discord.\n\n"
            "To also end the session on Discord's side, open Discord and go to "
            "User Settings > Devices.")

    def _show_about(self):
        messagebox.showinfo(
            f"About {APP_NAME}",
            f"{APP_NAME} {__version__}\n\n"
            "Collects Discord messages by scrolling a real browser at the pace "
            "of a person reading.\n\n"
            "Not affiliated with or endorsed by Discord. Automating a personal "
            "account is against Discord's terms: use at your own risk.\n\n"
            + (f"{HOMEPAGE}\n\n" if HOMEPAGE else "")
            + f"Your data: {DATA_DIR}")

    def _show_help(self):
        win = tk.Toplevel(self.root)
        win.title("Quick guide")
        win.geometry("720x640")
        win.transient(self.root)
        txt = tk.Text(win, wrap="word", padx=14, pady=10, font=("Segoe UI", 10))
        sb = ttk.Scrollbar(win, command=txt.yview)
        txt.config(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(fill="both", expand=True)
        txt.insert("1.0", HELP_TEXT)
        txt.config(state="disabled")

    # ---------------- persistence ----------------
    def _load_settings(self):
        if SETTINGS_FILE.exists():
            try:
                return json.loads(SETTINGS_FILE.read_text(encoding="utf-8-sig"))
            except Exception:
                pass
        return {}

    def _save_settings(self):
        try:
            SETTINGS_FILE.write_text(json.dumps(self.settings, indent=1), encoding="utf-8")
        except Exception:
            pass

    def _load_batches(self):
        if BATCHES_FILE.exists():
            try:
                batches = json.loads(BATCHES_FILE.read_text(encoding="utf-8-sig"))
                return [b for b in batches if b.get("name")]
            except Exception:
                pass
        return []

    def _save_batches(self):
        try:
            BATCHES_FILE.write_text(json.dumps(self.batches, indent=1),
                                    encoding="utf-8")
        except Exception as e:
            self._log(f"WARNING: could not save batches: {e}")

    def _save_queue(self):
        try:
            QUEUE_FILE.write_text(json.dumps(self.jobs, indent=1), encoding="utf-8")
        except Exception:
            pass

    def _load_queue(self):
        if not QUEUE_FILE.exists():
            return
        try:
            loaded = json.loads(QUEUE_FILE.read_text(encoding="utf-8-sig"))
        except Exception:
            return
        if not isinstance(loaded, list):
            return
        kept = []
        for j in loaded:
            if not jobs.fill_job(j):     # also adds the keys older versions lacked
                continue
            if j["status"] == RUNNING:   # app closed mid-job last time
                j["status"], j["note"] = PENDING, ""
            kept.append(j)
        self.jobs = kept
        self._refresh_tree()
        if kept:
            wait = sum(1 for j in kept if j["status"] in (PENDING, STOPPED, INCOMPLETE))
            self._log(f"Restored {len(kept)} job(s) from the last session "
                      f"({wait} still to run).")

    def _on_close(self):
        busy = self._busy()
        if busy:
            if not messagebox.askokcancel(
                    "Still running",
                    "A job is still running. Close anyway?\n\n"
                    "Everything captured so far is already in the archive. "
                    "The job carries on from there next time."):
                return
            self.stop_queue.set()
            self.stop_current.set()
            self.pause_event.clear()
        keep_awake.clear()
        self._save_queue()
        self.settings["last_batch"] = self.batch_var.get()
        self.settings["last_server"] = self.server_var.get()
        self.settings["batch_dated_dir"] = self.dated_var.get()
        self._save_settings()
        if not busy:
            self.store.close()
        self.root.destroy()


class BatchEditor(tk.Toplevel):
    """Modal editor for one batch: a name plus an ordered channel list.

    `result` stays None if the user cancelled, otherwise it is the saved batch.
    """

    def __init__(self, parent, batch, all_names, namer=None):
        super().__init__(parent)
        self.title("Edit batch" if batch.get("name") else "New batch")
        self.transient(parent)
        self.result = None
        self.namer = namer   # (guild id, channel id) -> label for a blank box
        self.original_name = batch.get("name", "")
        self.channels = [dict(c) for c in batch.get("channels", [])]
        self.taken = {n.lower() for n in all_names
                      if n.lower() != self.original_name.lower()}
        self.geometry("640x500")
        self.minsize(580, 460)

        frm = ttk.Frame(self)
        frm.pack(fill="both", expand=True, padx=10, pady=8)
        frm.columnconfigure(1, weight=1)
        frm.rowconfigure(2, weight=1)

        ttk.Label(frm, text="Batch name").grid(row=0, column=0, sticky="w", **PAD)
        self.name_var = tk.StringVar(value=self.original_name)
        ttk.Entry(frm, textvariable=self.name_var, width=32).grid(
            row=0, column=1, columnspan=2, sticky="w", **PAD)

        ttk.Label(frm, text="Channels (scraped in this order)").grid(
            row=1, column=0, columnspan=3, sticky="w", padx=8)
        self.lst = tk.Listbox(frm, height=9, selectmode="extended",
                              activestyle="none")
        self.lst.grid(row=2, column=0, columnspan=3, sticky="nsew", padx=(8, 0))
        sb = ttk.Scrollbar(frm, command=self.lst.yview)
        sb.grid(row=2, column=3, sticky="ns")
        self.lst.config(yscrollcommand=sb.set)

        btns = ttk.Frame(frm)
        btns.grid(row=3, column=0, columnspan=4, sticky="w", padx=6, pady=4)
        for txt, cmd in (("Move up", lambda: self._move(-1)),
                         ("Move down", lambda: self._move(1)),
                         ("Remove selected", self._remove)):
            ttk.Button(btns, text=txt, command=cmd).pack(side="left", padx=3)

        add = ttk.LabelFrame(frm, text="Add a channel")
        add.grid(row=4, column=0, columnspan=4, sticky="ew", padx=8, pady=6)
        add.columnconfigure(1, weight=1)
        ttk.Label(add, text="Channel link").grid(row=0, column=0, sticky="w", **PAD)
        self.link_var = tk.StringVar()
        link_entry = ttk.Entry(add, textvariable=self.link_var)
        link_entry.grid(row=0, column=1, columnspan=2, sticky="ew", **PAD)
        ttk.Label(add, text="Label").grid(row=1, column=0, sticky="w", **PAD)
        self.label_var = tk.StringVar()
        label_entry = ttk.Entry(add, textvariable=self.label_var, width=26)
        label_entry.grid(row=1, column=1, sticky="w", **PAD)
        ttk.Button(add, text="Add channel", command=self._add).grid(
            row=1, column=2, sticky="w", padx=4)
        ttk.Label(add, text="Right-click a channel in Discord -> Copy Link. "
                            "Blank label = the channel's real name.",
                  foreground="#777").grid(row=2, column=0, columnspan=3,
                                          sticky="w", padx=8, pady=(0, 4))
        for entry in (link_entry, label_entry):
            entry.bind("<Return>", lambda _e: self._add())

        foot = ttk.Frame(frm)
        foot.grid(row=5, column=0, columnspan=4, sticky="ew", padx=6, pady=4)
        ttk.Button(foot, text="Save batch", command=self._save).pack(side="right")
        ttk.Button(foot, text="Cancel", command=self.destroy).pack(side="right", padx=6)

        self._refresh()
        link_entry.focus_set()
        self.grab_set()
        self.wait_window(self)

    def _refresh(self):
        self.lst.delete(0, "end")
        for c in self.channels:
            self.lst.insert("end", f"{c['label']}   (channel {c['channel_id']})")

    def _add(self):
        try:
            guild_id, channel_id = parse_channel_link(self.link_var.get())
        except ValueError as e:
            messagebox.showerror("Check the link", str(e), parent=self)
            return
        if any(c["channel_id"] == channel_id for c in self.channels):
            messagebox.showinfo("Already there",
                                "That channel is already in this batch.", parent=self)
            return
        self.channels.append({
            "label": (self.label_var.get().strip()
                      or (self.namer(guild_id, channel_id) if self.namer
                          else jobs.placeholder(channel_id))),
            "guild_id": guild_id, "channel_id": channel_id})
        self.link_var.set("")
        self.label_var.set("")
        self._refresh()

    def _remove(self):
        for i in sorted(self.lst.curselection(), reverse=True):
            del self.channels[i]
        self._refresh()

    def _move(self, delta):
        idxs = sorted(self.lst.curselection(), reverse=delta > 0)
        moved = []
        for i in idxs:
            n = i + delta
            if 0 <= n < len(self.channels):
                self.channels[i], self.channels[n] = self.channels[n], self.channels[i]
                moved.append(n)
            else:
                moved.append(i)
        self._refresh()
        for i in moved:
            self.lst.selection_set(i)

    def _save(self):
        name = self.name_var.get().strip()
        if not name:
            messagebox.showerror("Name it", "Give the batch a name.", parent=self)
            return
        if name.lower() in self.taken:
            messagebox.showerror("Name taken",
                                 f"A batch called '{name}' already exists.",
                                 parent=self)
            return
        if not self.channels:
            messagebox.showerror("No channels",
                                 "Add at least one channel to the batch.", parent=self)
            return
        self.result = {"name": name, "channels": self.channels}
        self.destroy()


def selftest():
    """--selftest: verify the packaged app can drive a browser and open an
    archive.

    Writes the result to the data folder (a --windowed build has no console).
    """
    out = DATA_DIR / "selftest_result.txt"
    lines = [f"{APP_NAME} {__version__}"]
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            # channel="chromium" = the full browser the app itself drives,
            # run without a window (not the separate headless-only build).
            b = p.chromium.launch(channel="chromium", headless=True)
            lines.append("browser: " + p.chromium.executable_path)
            page = b.new_page()
            page.set_content("<h1 id=x>ok</h1>")
            lines.append("browser launched: OK")
            lines.append("dom read back: " + page.inner_text("#x"))
            b.close()
        lines.append("keep_awake: " + ("OK" if keep_awake.prevent_idle() else "FAILED"))
        keep_awake.clear()
        lines.append("profile dir: " + str(PROFILE_DIR) +
                     (" (exists)" if PROFILE_DIR.exists() else " (MISSING)"))

        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            st = Store(Path(tmp) / "t.db")
            st.put_messages("1", [{"id": "5", "ts": "2026-01-01T00:00:00+00:00",
                                   "author": "a", "author_id": "9", "content": "hi"}])
            st.add_coverage("1", 3, 9)
            ok = st.count_in("1", 0, 10) == 1 and not st.gaps("1", 4, 8)
            st.close()
            if not ok:
                raise RuntimeError("archive round trip failed")
            lines.append("archive: OK")
        lines.append("modules: " + ", ".join(
            m.__name__ for m in (jobs, scraper_core, user_history)))
        lines.append("RESULT: PASS")
    except Exception as e:
        lines.append(f"RESULT: FAIL — {type(e).__name__}: {e}")
    out.write_text("\n".join(lines), encoding="utf-8")


def _hold_running_mark():
    """Windows only: a named mark the installer looks for, so an update or
    uninstall asks you to close the app instead of overwriting a copy that
    may be in the middle of a queue. Released when the app exits."""
    try:
        import ctypes
        return ctypes.windll.kernel32.CreateMutexW(None, False, APP_NAME + "Running")
    except Exception:
        return None


def main():
    import multiprocessing
    multiprocessing.freeze_support()
    if "--selftest" in sys.argv:
        selftest()
        return
    mark = _hold_running_mark()     # noqa: F841 (kept alive until exit)
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    # The window icon: icon.ico beside this file (or inside the built app).
    icon = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "icon.ico"
    if icon.exists():
        try:
            root.iconbitmap(default=str(icon))
        except tk.TclError:
            pass
    try:
        ScraperApp(root)
    except Exception as e:
        messagebox.showerror("Could not start", f"{type(e).__name__}: {e}")
        raise
    root.mainloop()


if __name__ == "__main__":
    main()
