# Apple Photos Library Archive Sync

A safety-first macOS command-line tool that copies Apple Photos originals into
a human-readable `YYYY/MM/DD` archive. It resolves Photos UUID resource names
to their original camera/import filenames through `Photos.sqlite`.

> **Safety first:** runs are dry-runs by default. Nothing is copied or written
> unless `--apply` is supplied. Media is always copied byte-for-byte and is
> never modified in place.

## What it does

- Copies resources from a Photos library's `originals` directory to
  `TARGET/YYYY/MM/DD/original-filename`.
- Verifies existing media by size and SHA-256; identical files are skipped.
- Reports, but never overwrites, same-name files with differing content.
- Skips Photos `.AAE` edit-instruction sidecars.
- Optionally generates `<mediafile>.xmp` metadata sidecars with faces,
  keywords, GPS, and dates from the current Photos library.
- Uses atomic media, XMP, and JSON-report writes so interrupted writes do not
  leave partial output files.

## Requirements

- macOS with a locally accessible Apple Photos library.
- Python 3.10 or newer.
- No third-party dependency for `--no-sidecar` mode.
- [`osxphotos`](https://github.com/RhetTbull/osxphotos) for the default
  `--sidecar` mode.

Install sidecar support in an isolated environment:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
```

## Quick start

The checked-in defaults are:

| Option | Default |
| --- | --- |
| `--source` | `/Volumes/External/Photos Library.photoslibrary/originals` |
| `--database` | `/Volumes/External/Photos Library.photoslibrary/database/Photos.sqlite` |
| `--target` | `~/PhotosArchive` |

Always start with a small dry-run:

```sh
.venv/bin/python sync_photos_library.py --limit 20 --verbose
```

After reviewing the output, copy and create/update XMP sidecars:

```sh
.venv/bin/python sync_photos_library.py --apply --report sync-report.json
```

Use explicit paths for any other library or archive:

```sh
.venv/bin/python sync_photos_library.py \
  --source "/Volumes/Backup/Photos Library.photoslibrary/originals" \
  --database "/Volumes/Backup/Photos Library.photoslibrary/database/Photos.sqlite" \
  --target "$HOME/photosarchive-backup" \
  --limit 20
```

For the fastest plain archival copy with no metadata lookup dependency:

```sh
python3 sync_photos_library.py --apply --no-sidecar
```

## Usage examples

**Preview up to 20 assets without changing the archive** (the default mode):

```sh
.venv/bin/python sync_photos_library.py --limit 20 --verbose
```

**Copy all discovered assets and save a machine-readable report:**

```sh
.venv/bin/python sync_photos_library.py --apply --report sync-report.json
```

**Force an explicitly named dry-run**—useful in scripts that must never
write:

```sh
.venv/bin/python sync_photos_library.py --dry-run --limit 100
```

**Archive without XMP sidecars** using only the Python standard library:

```sh
python3 sync_photos_library.py --apply --no-sidecar
```

**Archive a library and database at custom locations:**

```sh
.venv/bin/python sync_photos_library.py \
  --source "/Volumes/PhotosBackup/Photos Library.photoslibrary/originals" \
  --database "/Volumes/PhotosBackup/Photos Library.photoslibrary/database/Photos.sqlite" \
  --target "$HOME/PhotosArchive" \
  --apply
```

## Sidecar metadata

`--sidecar` is enabled by default. It uses `osxphotos` to render current
Photos metadata into `<mediafile>.xmp`, while the media itself remains a plain
source-byte copy. Existing sidecars are rendered and compared each run, then
rewritten only if content changed.

Live Photo video companions do not receive their own XMP sidecar; metadata is
associated with the still image, matching `osxphotos` behavior.

Legacy media written by older `osxphotos export --exiftool` workflows may have
different bytes than their raw source. When a matching XMP sidecar exists, the
tool classifies that media as `legacy`, leaves it untouched, and refreshes its
sidecar. A mismatch without a sidecar is a `conflict` and is never overwritten.

`--exiftool` and `--no-exiftool` remain supported as deprecated aliases for
`--sidecar` and `--no-sidecar`; they do **not** embed metadata into media.

## Operational behavior

- The source library is read-only. If `Photos.sqlite` is locked, the script
  reads a temporary local copy of the database and WAL sidecars.
- Destination media and XMP paths that are symlinks are rejected rather than
  followed or replaced.
- The target cannot be inside the source directory.
- A JSON report is printed to standard output. `--report PATH` additionally
  writes it atomically to `PATH`.
- Exit status is `0` for a clean run and `1` for conflicts, failures, or
  unresolved source/database issues. `legacy` and sidecar updates are not
  errors.

The repository ignores `my-running-command.md` and `sync-report.json` so
machine-specific commands and operational reports are not published.

## Development

Install test dependencies and run the suite:

```sh
python3 -m pip install -r requirements-dev.txt
python3 -m pytest -q
python3 -m compileall -q sync_photos_library.py
```

Tests use an isolated temporary SQLite fixture and never require a real Photos
library. CI runs the same test suite on supported Python versions.

## Contributing

Please keep changes focused, preserve the dry-run-by-default guarantee, and
add regression coverage for behavior changes. Do not commit Photos databases,
archives, generated reports, or machine-specific command files.

## License

Distributed under the [MIT License](LICENSE).
