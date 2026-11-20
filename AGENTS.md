# AGENTS.md — iCloud / Photos Library Archive Sync

## Project Overview
Single-file Python tool (`sync_photos_library.py`) that archives Apple Photos originals into a human-readable `YYYY/MM/DD` directory tree. It resolves UUID-named source files back to camera filenames via `Photos.sqlite`, and optionally writes Photos.app metadata (faces, GPS, keywords) into a `<mediafile>.xmp` sidecar using `osxphotos`'s `SidecarWriter` — the media file itself is always a plain byte-for-byte copy and is **never** modified.

**Default is always dry-run.** Requires `--apply` to actually copy files.

## Architecture
| Component | File | Role |
|---|---|---|
| Main script | `sync_photos_library.py` | All logic (single module) |
| Tests | `test_sync_photos_library.py` | pytest suite; uses `tmp_path` fixtures |
| Runtime deps | `requirements.txt` | `osxphotos` only (needed for `--sidecar` mode) |

### Key data flow
1. `load_assets(database, source)` — SQLite query on `ZASSET` + `ZADDITIONALASSETATTRIBUTES` → resolves UUID filenames to original names + capture timestamps
2. `sync(assets, target, apply, sidecar_renderer)` — copies to `target/YYYY/MM/DD/<original_name>` byte-for-byte; SHA-256 deduplication; conflict detection; optional sidecar generation
3. `SidecarRenderer.render()` — renders XMP sidecar content via `osxphotos.sidecars.SidecarWriter.xmp_sidecar()` from current Photos metadata; caller compares/writes it, no media file involved

### Timestamp epoch: `float(timestamp) + 978307200`
Apple Core Data stores dates as seconds since **2001-01-01**, not Unix epoch. The offset `978307200` is not a magic number — it is the fixed conversion constant.

### `.AAE` files are always skipped
They are Photos edit-instruction side-cars and intentionally excluded (see `load_assets`).

### Recently Deleted assets are always skipped
`load_assets` excludes assets with a non-zero `ZTRASHEDSTATE`. Their originals stay on disk until Photos purges them, so `load_assets` keeps their UUIDs in `trashed` and skips those files silently; otherwise they would be misreported as having no database record. Archived copies of items trashed later are left in place.

### Missing originals are reported (`scan_library`)
`scan_library()` returns a `LibraryScan` (`assets`, `issues`, `missing_originals`); `load_assets()` is a thin `(assets, issues)` wrapper kept for callers. A non-trashed asset whose original belongs in `originals` but has no file there (typically iCloud "Optimize Mac Storage") is listed in `missing_originals`, which makes `main()` exit `1`. Assets that never live in `originals` are excluded via `_NON_LOCAL_ORIGINAL_CONDITIONS` (shared album, referenced file, moment share, Shared with You); each condition applies only if its column exists. The check covers the whole library within the capture-date range, even past `--limit` (remaining paths are name-matched without a stat) and regardless of `--after`.

### AppleDouble cleanup (`remove_appledouble_files`)
macOS stores xattrs as `._*` files on filesystems without native xattr support. On the fuse-t NFS `rclone mount`, these appear even with `--noappledouble` and upload as real objects. After an `--apply` run, `sync()` removes them from each written directory and its ancestors up to the target, checking the AppleDouble magic number first. Directories it did not write to are not cleaned.

### Lock-aware SQLite reads (`open_database_readonly`)
Reading `Photos.sqlite` directly via `mode=ro` blocks/retries against sqlite's busy-timeout when Photos.app or `photoanalysisd` holds the DB open in WAL mode — slow, especially over a slow/external volume. Mirroring `osxphotos.sqlite_utils.sqlite_open_ro_with_temp_copy`, `open_database_readonly()` first probes with `_sqlite_db_is_locked()`; only if locked does it copy the db plus its `-wal`/`-shm` side-cars to a local temp dir and query that snapshot instead. Note: `osxphotos.PhotosDB` (used by `SidecarRenderer`) is a much heavier full-library load (persons, faces, albums, moments) — not a faster alternative to our own lean 3-column query.

## Developer Workflows

### Run tests
```sh
pytest                         # all tests
pytest -k test_load_and_sync   # single test
```
No external processes or real Photos library required — `StubSidecarRenderer` in the test file duck-types `SidecarRenderer`, returning deterministic `<xmp uuid='...'>{content}</xmp>` strings so tests can simulate Photos metadata changing between runs.

### Setup for --sidecar (metadata) mode
```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```
No `exiftool` binary is required — sidecar generation is pure `osxphotos` (Python), unlike the old `--exiftool` embedding approach it replaced.

### Run the script
```sh
# Dry-run preview (default):
python3 sync_photos_library.py --limit 20

# Apply with JSON report:
.venv/bin/python sync_photos_library.py --apply --report sync-report.json

# Plain copy, no third-party deps:
.venv/bin/python sync_photos_library.py --apply --no-sidecar
```

Default paths (override with `--source`, `--database`, `--target`):
- Source: `/Volumes/External/Photos Library.photoslibrary/originals`
- DB: `/Volumes/External/Photos Library.photoslibrary/database/Photos.sqlite`
- Target: `~/PhotosArchive`

## Conventions & Patterns

### Media file is never modified; sidecar is fully owned/regenerated by us
Earlier design baked metadata into each copy's own EXIF/XMP tags via `exiftool`, which required byte-matching a fresh bake against the archive to detect "already done" — this proved fundamentally unstable (see `TASK-metadata-sidecar-redesign.md`) since Photos metadata (face names, keywords) keeps changing after export, so a byte-exact match could never be relied on long-term. The current design instead: (1) always copies the media file byte-for-byte from the raw Photos original — trivial, 100%-reliable conflict detection via plain size+SHA-256; (2) renders the `.xmp` sidecar fresh from *current* Photos metadata on every run and rewrites it only if content differs from what's on disk. No xattr or extra state file is needed for the sidecar either — the comparison is a direct string compare against the existing `.xmp` file's content (see `sidecar_path()`, `sync()`).

### Legacy media files (from the old `--exiftool` embedding era)
If an existing destination's bytes differ from the raw Photos original (because a prior *real* `osxphotos export --exiftool` run embedded metadata into it) and it has its own `.xmp` sidecar (or, for a companion resource with `asset.part is not None`, `has_sibling_sidecar()` finds one on a same-stem sibling), `sync()` treats it as `legacy` and leaves the media bytes untouched rather than flagging a conflict — the sidecar for it is still refreshed normally. Only a byte mismatch with **no** accompanying sidecar is treated as a genuine `conflict`.

### Live Photo video companions never get their own sidecar
Confirmed empirically against the real archive (`TASK-metadata-sidecar-redesign.md`): real `osxphotos export --sidecar XMP` only ever writes a sidecar for the still-image half of a Live Photo pair (e.g. `IMG_1234.HEIC.xmp`) — never for the paired `.mov` video (`asset.part is not None` in `load_assets`'s UUID matching, e.g. `..._3.mov`). Two consequences in `sync()`: (1) `has_sibling_sidecar(destination)` also checks for a sidecar on any sibling file sharing the same stem (`IMG_1234.*.xmp`) before falling back to `conflict`, so a legacy `.mov` is correctly trusted via its paired image's sidecar — this sibling fallback applies only to companion resources, so e.g. a RAW sidecar cannot hide a genuine conflict on the JPEG of a RAW+JPEG pair; (2) sidecar generation is skipped entirely for `asset.part is not None` so we don't create a `*.mov.xmp` that wouldn't exist in a real osxphotos export. Without both fixes, every Live Photo video with legacy-modified bytes was misreported as a conflict (11/100 in a real `--limit 100` sample before the fix, 0/100 after).

### `--sidecar`/`--no-sidecar` with deprecated `--exiftool`/`--no-exiftool` aliases
Both flag pairs map to the same `args.sidecar` dest via a single `argparse.BooleanOptionalAction` declaration (`parser.add_argument("--sidecar", "--exiftool", dest="sidecar", ...)`), which auto-generates all four of `--sidecar/--no-sidecar/--exiftool/--no-exiftool`. `parse_args()` prints a deprecation warning to stderr if either `--exiftool` or `--no-exiftool` is present in argv, since those names no longer accurately describe the behavior (metadata is never embedded into the media file anymore).

### Atomic writes
`copy_atomic()` (media) and `write_text_atomic()` (sidecar) both write to a temp file in the same directory, then call `os.replace()` — ensures no partial files land in the archive.

### Progress reporting (`--verbose`, `sync()`'s `progress_every`)
`sync()` prints to stderr as it goes (all `flush=True`, since stderr can still be block-buffered when piped/redirected): a per-asset line if `verbose=True` (`--verbose`/`-v`), otherwise a periodic heartbeat every `progress_every` assets (default 50) showing running counts. Conflicts and failures are always printed immediately (`force=True` in the internal `_progress()` closure) regardless of verbosity, since they're rare and need attention. Separately, `SidecarRenderer.__init__` prints before/after loading `osxphotos.PhotosDB()` — that full-library load (persons, faces, albums, moments) is the dominant one-time cost of a run (minutes on a large library) and has no incremental per-file progress of its own; osxphotos only relays a handful of coarse phase messages via its own `verbose` callback.

### Exit code semantics
Returns `1` if any conflicts, failures, or issues exist; `0` on clean run. `legacy` and `sidecar_written`/`sidecar_skipped` counts never affect the exit code.

### Test fixture pattern
`make_fixture(tmp_path)` builds a minimal in-memory SQLite DB + source files. Use it as the base; add rows with `sqlite3.connect(database)` for edge-case tests. `StubSidecarRenderer` accepts a `default` string to distinguish sidecar "versions" across runs in assertions (e.g. simulating Photos metadata changing between syncs).

