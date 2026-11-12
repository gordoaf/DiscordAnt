# Changelog

## 1.0.4 — 2026-11-12

Three long-standing problems found by a review of 1.0.3. None adds any
request to Discord.

- An older copy of a message can no longer replace a newer edit. When two of
  Discord's answers held the same message and the older one arrived last, the
  archive went back to the text from before the edit. The time of the edit is
  now kept with each message and compared first.
- A batch's "collected through" day is the day the job's dates really ended
  on. The app stops a minute short of "now", so a job started in the first
  minute after midnight UTC had only collected the day before, yet recorded
  the new day, and the next top-up skipped that last minute.
- A picture already saved for one message is listed for every other message
  that shows the same picture (a link posted twice, a forwarded attachment).
  The browser loads such a picture once, so the later messages used to be
  left without it.
- A declared length is no longer trusted for a picture that is packed on the
  wire, and the log says how many browser-shown pictures were left uncopied
  because their size could not be confirmed.

## 1.0.3 — 2026-11-12

- Start over on a user job is now recorded in the archive the moment the job
  begins. Before, a job stopped (or cut off by its time limit, or a crash)
  before its first search answered lost the "read everything again" part:
  the next run kept the old text of edited messages and still said complete.
- A picture that finishes loading before the message list that mentions it
  has been read is no longer dropped; it waits for that list. This could lose
  previews, mostly of pictures the browser already had.
- A browser-loaded picture is only copied once its size is known to be within
  the limit, also when the server does not declare a length, and at most four
  are copied at a time.
- A batch last run by 1.0.0 still offers the day it was last queued, but that
  day is now checked against the archive: the log names every channel that
  has nothing collected just before it, so a missed run is visible before a
  top-up skips it.
- Wording: what a message replied to is kept as a short quote, not as the
  full message, and the README and help now say so.

Known limit, with a workaround: if **Start over** was pressed on a channel
job in 1.0.1 and the job was stopped before it finished, updating treats the
rest of that range as already read (1.0.1 kept no note of the request, so it
cannot be told apart from an ordinary unfinished job). Press **Start over**
on that job again.

## 1.0.2 — 2026-11-12

Fixes for problems introduced in 1.0.1. Nothing here changes how fast or how
often the app talks to Discord, except that it now does so less.

- Updating no longer forgets what has already been collected. 1.0.1 set the
  record of fully read history aside, so every job over old dates scrolled
  Discord again. The record is kept, and an archive that went through 1.0.1
  gets it back the first time this version opens it.
- Saved embed pictures stay listed after the update when they are still
  certainly the right ones (1.0.1 unlisted all of them).
- User jobs "up to now" finish as done. The newest few minutes, which
  Discord's search cannot see yet, are collected by the next run and the
  output says where the job stopped.
- A message near the start or end of a channel no longer keeps a user job
  open for ever, and a message deleted since it was found is not asked for
  again. Before, such a job revisited the message on every run, and five in a
  row stopped it reaching the remaining channels.
- When search results move while they are paged (the person posts, or a
  message is deleted), the pages already read are kept and the search
  continues from there instead of the whole pass being thrown away.
- An unknown username in a job "up to now" is reported as failed again,
  instead of being retried on every start.
- Batches offer "since last time" again: a batch last run by 1.0.0 offers
  the day it was last queued, and one mistaken queue with a blank end date no
  longer switches the suggestion off for good. Failed or waiting jobs still
  never move the date.
- Pictures seen while scrolling are saved from the browser's own copy again
  instead of being downloaded a second time.
- Start queue no longer refuses when a finished job and a new job share an
  output file.
- A saved batches file with one odd value no longer loads as empty and gets
  overwritten: what can be read is kept, and the file as it was is set aside
  as `batches.json.rejected`.
- The release tools and their tests also run on Python 3.10.

## 1.0.1 — 2026-11-11

- Prevent export collisions across resumable jobs, Windows path aliases and
  repeated batch labels. Protect existing or externally edited output files.
- Verify search identity, request timing, sort, bounds and pagination before
  recording completion. Reject unrelated context responses.
- Refresh edited user messages on Start over, with durable interrupted-refresh
  progress. Report the deferred search-index tail and actual collection bounds.
- Reconcile removed/replaced pictures, preserve per-message attachment
  references and prevent stale thumbnails from replacing full images.
- Preserve legacy messages/files while migrating unsafe completion and image
  indexes. Revalidate old completion evidence once.
- Base batch top-ups on successful contiguous collection; keep failures visible.
- Enforce Stop and time budgets during startup, login, search and navigation.
- Escape transcript metadata, validate exact Discord/CDN origins and use
  bounded raster-only image downloads without browser/login state.
- Validate saved state, recover backups, save atomically and prevent concurrent
  Windows app instances.
- Add offline regressions, Windows CI, hash-locked dependencies, installer
  checksums, source/build provenance and an optional provisioned signing hook.

The tests cover synthetic failures and an isolated package smoke test. No live
Discord crawl is part of the automated regression suite.
