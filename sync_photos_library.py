#!/usr/bin/env python3
"""Safely sync Apple Photos library originals into a date-organized archive."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol, TypedDict

UUID_RE = re.compile(r"^(?P<uuid>[0-9A-Fa-f-]{36})(?:_(?P<part>\d+))?(?:\.[^.]+)?$")
DEFAULT_SOURCE = Path("/Volumes/External/Photos Library.photoslibrary/originals")
DEFAULT_DB = Path("/Volumes/External/Photos Library.photoslibrary/database/Photos.sqlite")
DEFAULT_TARGET = Path.home() / "PhotosArchive"

# CLI flags kept as deprecated aliases for --sidecar/--no-sidecar.
_DEPRECATED_SIDECAR_FLAGS = ("--exiftool", "--no-exiftool")


@dataclass
class Asset:
    uuid: str
    source: Path
    original_name: str
    captured: datetime
    part: str | None = None

    @property
    def destination_name(self) -> str:
        if self.part is None:
            return self.original_name
        suffix = self.source.suffix
        return f"{Path(self.original_name).stem}{suffix}"

    @property
    def relative_destination(self) -> Path:
        return Path(f"{self.captured.year:04d}") / f"{self.captured.month:02d}" / f"{self.captured.day:02d}" / self.destination_name


class SidecarRendererProtocol(Protocol):
    """The minimal sidecar renderer interface used by ``sync``."""

    def render(self, asset: Asset) -> str: ...


class SyncResult(TypedDict, total=False):
    counts: dict[str, int]
    copied: list[str]
    skipped: list[str]
    legacy: list[str]
    conflicts: list[str]
    failures: list[str]
    sidecar_written: list[str]
    sidecar_skipped: list[str]
    mode: str
    source: str
    target: str
    issues: list[str]


def parse_after(value: str) -> float:
    """Parse an ISO-8601 cutoff as a Unix timestamp.

    A date-only value starts at local midnight. Datetimes with an explicit UTC
    offset are also accepted; naive datetimes use the local timezone.
    """
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid ISO-8601 date/time: {value!r} (for example, 2024-01-01 or 2024-01-01T12:30:00)"
        ) from exc


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--database", type=Path, default=DEFAULT_DB)
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    parser.add_argument("--apply", action="store_true", help="copy files (default is dry-run)")
    parser.add_argument("--dry-run", action="store_true", help="preview changes without copying")
    parser.add_argument("--limit", type=int, help="process at most this many source media files")
    parser.add_argument(
        "--after",
        type=parse_after,
        metavar="ISO_DATETIME",
        help=(
            "only process source media created or modified after this ISO-8601 date/time "
            "(for example, 2024-01-01 or 2024-01-01T12:30:00)"
        ),
    )
    parser.add_argument("--report", type=Path, help="write the JSON report to this path")
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="print a line to stderr for every asset processed, not just periodic progress",
    )
    parser.add_argument(
        "--sidecar",
        "--exiftool",
        dest="sidecar",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "write Photos.app metadata (faces, keywords, GPS, dates) into a "
            "<mediafile>.xmp sidecar next to each archived copy, like "
            "`osxphotos export --sidecar XMP`; requires `pip install osxphotos`. "
            "The media file itself is always a plain byte-for-byte copy of the "
            "Photos original -- metadata is never embedded into it. Enabled by "
            "default; pass --no-sidecar to skip sidecar generation entirely. "
            "(--exiftool/--no-exiftool are accepted as deprecated aliases.)"
        ),
    )
    args = parser.parse_args(argv)
    used_argv = argv if argv is not None else sys.argv[1:]
    if any(flag in used_argv for flag in _DEPRECATED_SIDECAR_FLAGS):
        print(
            "warning: --exiftool/--no-exiftool are deprecated aliases for "
            "--sidecar/--no-sidecar (metadata is written to an .xmp sidecar, "
            "never embedded into the media file); please switch to --sidecar/--no-sidecar",
            file=sys.stderr,
        )
    return args


def _sqlite_connect_ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1)


def _sqlite_db_is_locked(path: Path) -> bool:
    """Mirrors osxphotos.sqlite_utils.sqlite_db_is_locked: a quick read-only
    query fails if Photos.app (or photoanalysisd) holds an exclusive lock."""
    try:
        with _sqlite_connect_ro(path) as connection:
            connection.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
        return False
    except sqlite3.Error:
        return True


@contextlib.contextmanager
def open_database_readonly(database: Path):
    """Open `database` read-only, yielding a connection.

    If the live Photos.sqlite is locked (Photos.app or photoanalysisd has it
    open in WAL mode), querying it directly over `mode=ro` repeatedly hits
    sqlite's busy-timeout retries -- painfully slow, especially when `database`
    lives on a slow/external volume. osxphotos avoids this
    (`sqlite_open_ro_with_temp_copy`) by copying the db plus its `-wal`/`-shm`
    side-car files to a local temp dir first and querying that snapshot
    instead. We do the same here, but only pay the copy cost when actually
    locked.
    """
    if not _sqlite_db_is_locked(database):
        with _sqlite_connect_ro(database) as connection:
            yield connection
        return

    with tempfile.TemporaryDirectory(prefix="sync_photos_library_") as tempdir:
        temp_db = Path(tempdir) / database.name
        for suffix in ("", "-wal", "-shm"):
            source_file = database.with_name(database.name + suffix)
            if source_file.exists():
                shutil.copyfile(source_file, temp_db.with_name(temp_db.name + suffix))
        with _sqlite_connect_ro(temp_db) as connection:
            yield connection


def source_is_created_or_modified_after(stat_result: os.stat_result, after: float) -> bool:
    """Return whether source media was created or modified strictly after ``after``.

    macOS exposes file creation time as ``st_birthtime``. On filesystems that
    do not provide it, the modification time remains a safe, portable filter.
    """
    created = getattr(stat_result, "st_birthtime", None)
    return stat_result.st_mtime > after or (isinstance(created, (int, float)) and created > after)


def load_assets(
    database: Path,
    source: Path,
    limit: int | None = None,
    after: float | None = None,
) -> tuple[list[Asset], list[str]]:
    """Read Photos metadata and match UUID-named resources in originals.

    When ``after`` is set, include only sources created or modified strictly
    after that Unix timestamp.
    """
    if limit == 0:
        return [], []

    assets: dict[str, tuple[str, datetime]] = {}
    issues: list[str] = []
    with open_database_readonly(database) as connection:
        rows = connection.execute(
            """SELECT a.ZUUID, COALESCE(x.ZORIGINALFILENAME, a.ZFILENAME), a.ZDATECREATED
               FROM ZASSET a LEFT JOIN ZADDITIONALASSETATTRIBUTES x ON x.ZASSET=a.Z_PK
               WHERE a.ZUUID IS NOT NULL AND a.ZDATECREATED IS NOT NULL"""
        )
        for uuid, name, timestamp in rows:
            if not name or timestamp is None:
                continue
            try:
                captured = datetime.fromtimestamp(float(timestamp) + 978307200)
            except (TypeError, ValueError, OverflowError, OSError):
                continue
            key = str(uuid).upper()
            if key in assets:
                issues.append(f"duplicate ZUUID in database, keeping first match: {key}")
                continue
            original_name = Path(str(name)).name
            if original_name in {"", ".", ".."} or "\x00" in original_name:
                issues.append(f"invalid original filename for {key}: {name!r}")
                continue
            assets[key] = (original_name, captured)

    matched: list[Asset] = []
    # Photos stores originals in hexadecimal shard directories; recurse so the
    # tool also works with libraries that add another level of sharding.
    for path in sorted(source.rglob("*")):
        if not path.is_file() or path.name.startswith(".") or path.suffix.lower() == ".aae":
            continue
        match = UUID_RE.match(path.name)
        if not match:
            issues.append(f"unrecognized source filename: {path}")
            continue
        uuid = match.group("uuid").upper()
        metadata = assets.get(uuid)
        if metadata is None:
            issues.append(f"no Photos database record for: {path}")
            continue
        if after is not None:
            try:
                if not source_is_created_or_modified_after(path.stat(), after):
                    continue
            except OSError as exc:
                issues.append(f"unable to read source timestamps for {path}: {exc}")
                continue
        name, captured = metadata
        matched.append(Asset(uuid, path, name, captured, match.group("part")))
        if limit is not None and len(matched) >= limit:
            break
    return matched, issues


def sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sidecar_path(destination: Path) -> Path:
    """Path of the XMP sidecar for an archived media file, e.g.
    `IMG_1234.HEIC` -> `IMG_1234.HEIC.xmp` (matches osxphotos's own
    `--sidecar XMP` naming convention)."""
    return destination.with_name(destination.name + ".xmp")


def has_sibling_sidecar(destination: Path) -> bool:
    """True if `destination` itself has a sidecar, or a sibling file sharing
    its stem (but a different extension) does.

    Live Photo video companions (e.g. `IMG_1234.mov` paired with
    `IMG_1234.HEIC`) never get their own `--sidecar XMP` file from osxphotos
    -- only the still-image half of the pair does. Without this, every
    legacy Live Photo video whose bytes were modified in place by a prior
    real `osxphotos export --exiftool` run would be misreported as a
    conflict, even though it's really just an artifact of how osxphotos
    pairs sidecars to the photo, not the video (confirmed empirically: see
    TASK-metadata-sidecar-redesign.md).
    """
    if sidecar_path(destination).exists():
        return True
    return any(destination.parent.glob(f"{destination.stem}.*.xmp"))


class SidecarRenderer:
    """Renders the XMP sidecar for an asset from current Photos.app metadata
    (faces, keywords, GPS, dates), mirroring `osxphotos export --sidecar XMP`.

    Unlike the old `--exiftool` baking approach, this never touches the media
    file itself -- only the returned sidecar string, which the caller is
    responsible for comparing/writing. Because sidecars are fully owned and
    regenerated by us on every run, there's no need for byte-matching against
    a third party or any extra state (e.g. an xattr) to track "already done".
    """

    def __init__(self, database: Path):
        try:
            import osxphotos
            from osxphotos.exportoptions import ExportOptions
            from osxphotos.sidecars import SidecarWriter
        except ImportError as exc:
            raise SystemExit(
                "--sidecar requires the 'osxphotos' package (pip install osxphotos)"
            ) from exc
        self._sidecar_writer_cls = SidecarWriter
        # use_persons_as_keywords matches this archive's existing convention
        # (set by the prior `osxphotos export --exiftool --sidecar XMP` runs):
        # photos with face-detected persons but no explicit Photos keywords
        # still get Subject/TagsList populated from the person names.
        self._options = ExportOptions(use_persons_as_keywords=True)
        # osxphotos.PhotosDB() is a full-library load (persons, faces, albums,
        # moments, keywords, shared libraries...), which can take *minutes* on
        # a large library -- it is not a quick per-file lookup. `verbose`
        # relays osxphotos's own coarse phase-by-phase progress messages
        # (e.g. "Processing persons in photos.", "Processing albums.") so the
        # wait isn't silent; flush=True since stderr can still be
        # block-buffered when piped/redirected.
        print("Loading full Photos database for sidecar generation (osxphotos) "
              "-- this can take several minutes on a large library...", file=sys.stderr, flush=True)
        self._photosdb = osxphotos.PhotosDB(
            dbfile=str(database),
            verbose=lambda msg: print(msg, file=sys.stderr, flush=True),
        )
        print("Photos database loaded.", file=sys.stderr, flush=True)

    def render(self, asset: Asset) -> str:
        """Return the XMP sidecar content for `asset` from current Photos metadata."""
        photo = self._photosdb.get_photo(asset.uuid)
        if photo is None:
            raise LookupError(f"no PhotoInfo for uuid {asset.uuid}")
        extension = Path(asset.destination_name).suffix.lstrip(".") or None
        return self._sidecar_writer_cls(photo).xmp_sidecar(options=self._options, extension=extension)


def copy_atomic(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    os.close(fd)
    temporary_path = Path(temporary)
    try:
        shutil.copyfile(source, temporary_path)
        shutil.copystat(source, temporary_path, follow_symlinks=False)
        os.replace(temporary_path, destination)
    finally:
        temporary_path.unlink(missing_ok=True)


def write_text_atomic(destination: Path, content: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


def sync(
    assets: list[Asset],
    target: Path,
    apply: bool,
    sidecar_renderer: SidecarRendererProtocol | None = None,
    verbose: bool = False,
    progress_every: int = 50,
) -> SyncResult:
    if progress_every <= 0:
        raise ValueError("progress_every must be positive")
    # Process the archive chronologically regardless of filesystem discovery or
    # caller-provided order. The remaining fields make equal capture times
    # deterministic without mutating the caller's list.
    assets = sorted(
        assets,
        key=lambda asset: (asset.captured, asset.uuid, asset.part or "", str(asset.source)),
    )
    counts = Counter({
        "copied": 0,
        "skipped": 0,
        "legacy": 0,
        "conflict": 0,
        "failure": 0,
        "sidecar_written": 0,
        "sidecar_skipped": 0,
    })
    details: dict[str, list[str]] = {
        key: []
        for key in (
            "copied", "skipped", "legacy", "conflicts", "failures",
            "sidecar_written", "sidecar_skipped",
        )
    }
    planned: dict[Path, Asset] = {}
    total = len(assets)

    def _progress(label: str, detail: str = "", force: bool = False) -> None:
        """Report progress to stderr: every asset if verbose, otherwise a
        periodic heartbeat every `progress_every` assets (plus always for
        conflicts/failures via force=True, since those need attention)."""
        if verbose:
            suffix = f": {detail}" if detail else ""
            print(f"[{index}/{total}] {label}{suffix}", file=sys.stderr, flush=True)
        elif force or index == total or index % progress_every == 0:
            print(
                f"[{index}/{total}] copied={counts['copied']} skipped={counts['skipped']} "
                f"legacy={counts['legacy']} conflicts={counts['conflict']} "
                f"failures={counts['failure']} sidecar_written={counts['sidecar_written']} "
                f"sidecar_skipped={counts['sidecar_skipped']}",
                file=sys.stderr, flush=True,
            )

    for index, asset in enumerate(assets, start=1):
        destination = target / asset.relative_destination
        prior = planned.get(destination)
        if prior is not None and prior.source != asset.source:
            counts["conflict"] += 1
            details["conflicts"].append(f"duplicate destination: {destination} ({prior.source}, {asset.source})")
            _progress("conflict", f"duplicate destination {destination}", force=True)
            continue
        planned[destination] = asset

        try:
            if destination.is_symlink():
                counts["conflict"] += 1
                details["conflicts"].append(f"destination is a symlink: {destination}")
                _progress("conflict", f"destination is a symlink: {destination}", force=True)
                continue
            if destination.exists():
                same_as_source = (
                    destination.stat().st_size == asset.source.stat().st_size
                    and sha256(destination) == sha256(asset.source)
                )
                if same_as_source:
                    counts["skipped"] += 1
                    details["skipped"].append(str(destination))
                    _progress("skipped", str(destination))
                elif has_sibling_sidecar(destination):
                    # Media file was modified in place by a prior real
                    # `osxphotos export --exiftool` run (bytes differ from the
                    # raw Photos original by design). Trust it because a
                    # sidecar already exists for it (or, for a Live Photo
                    # video companion, for its paired still image) -- don't
                    # try to byte-match against historical exiftool output,
                    # which is inherently unstable as Photos metadata keeps
                    # changing.
                    counts["legacy"] += 1
                    details["legacy"].append(str(destination))
                    _progress("legacy", str(destination))
                else:
                    counts["conflict"] += 1
                    details["conflicts"].append(f"different content: {destination} <- {asset.source}")
                    _progress("conflict", f"different content {destination}", force=True)
                    continue
            else:
                if apply:
                    copy_atomic(asset.source, destination)
                counts["copied"] += 1
                details["copied"].append(str(destination))
                _progress("copied", str(destination))

            if sidecar_renderer is not None and asset.part is None:
                # Live Photo video companions (asset.part is not None, e.g.
                # the "_3.mov" resource) never get their own sidecar from
                # real osxphotos --sidecar XMP either -- only the paired
                # still image does (confirmed empirically). Skip them here so
                # we don't create a `*.mov.xmp` file that wouldn't otherwise
                # exist in the archive.
                dest_sidecar = sidecar_path(destination)
                if dest_sidecar.is_symlink():
                    raise OSError(f"refusing to read or replace symlinked sidecar: {dest_sidecar}")
                new_content = sidecar_renderer.render(asset)
                existing_content = dest_sidecar.read_text(encoding="utf-8") if dest_sidecar.exists() else None
                if existing_content != new_content:
                    if apply:
                        write_text_atomic(dest_sidecar, new_content)
                    counts["sidecar_written"] += 1
                    details["sidecar_written"].append(str(dest_sidecar))
                    _progress("sidecar_written", str(dest_sidecar))
                else:
                    counts["sidecar_skipped"] += 1
                    details["sidecar_skipped"].append(str(dest_sidecar))
        except OSError as exc:
            counts["failure"] += 1
            details["failures"].append(f"{asset.source}: {exc}")
            _progress("failure", f"{asset.source}: {exc}", force=True)
        except Exception as exc:  # noqa: BLE001 - keep syncing other assets if one fails
            counts["failure"] += 1
            details["failures"].append(f"{asset.source}: {exc}")
            _progress("failure", f"{asset.source}: {exc}", force=True)
    return {"counts": dict(counts), **details}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.apply and args.dry_run:
        raise SystemExit("--apply and --dry-run are mutually exclusive")
    source, database, target = args.source.expanduser(), args.database.expanduser(), args.target.expanduser()
    if not source.is_dir():
        raise SystemExit(f"source directory does not exist: {source}")
    if not database.is_file():
        raise SystemExit(f"Photos database does not exist: {database}")
    if args.limit is not None and args.limit < 0:
        raise SystemExit("--limit must be non-negative")
    try:
        target.resolve().relative_to(source.resolve())
    except ValueError:
        pass
    else:
        raise SystemExit("target directory must not be inside the source directory")
    print("Scanning source directory and Photos database for assets to sync...", file=sys.stderr, flush=True)
    assets, issues = load_assets(database, source, args.limit, args.after)
    print(f"Found {len(assets)} assets to sync ({len(issues)} issues).", file=sys.stderr, flush=True)
    sidecar_renderer = None
    if args.sidecar:
        sidecar_renderer = SidecarRenderer(database)
    result = sync(assets, target, args.apply, sidecar_renderer, verbose=args.verbose)
    result["mode"] = "apply" if args.apply else "dry-run"
    result["source"] = str(source)
    result["target"] = str(target)
    result["issues"] = issues
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if args.report:
        write_text_atomic(
            args.report.expanduser(),
            json.dumps(result, indent=2, ensure_ascii=False) + "\n",
        )
    return 1 if result["counts"].get("conflict", 0) or result["counts"].get("failure", 0) or issues else 0


if __name__ == "__main__":
    sys.exit(main())
