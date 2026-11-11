# Changelog

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
