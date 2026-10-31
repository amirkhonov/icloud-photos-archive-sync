import os
import sqlite3
import time
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from sync_photos_library import (
    DEFAULT_TARGET,
    Asset,
    has_sibling_sidecar,
    load_assets,
    parse_args,
    sidecar_path,
    source_is_created_or_modified_after,
    sync,
)


class StubSidecarRenderer:
    """Duck-typed stand-in for SidecarRenderer so tests avoid loading a real
    Photos database. Returns deterministic content per asset uuid, optionally
    parameterized so tests can simulate Photos metadata changing between runs."""

    def __init__(self, default: str = "v1"):
        self.default = default
        self.rendered: list[str] = []

    def render(self, asset) -> str:
        self.rendered.append(asset.uuid)
        return f"<xmp uuid='{asset.uuid}'>{self.default}</xmp>"


def test_default_target_is_neutral_archive_directory():
    assert DEFAULT_TARGET == Path.home() / "PhotosArchive"
    assert parse_args([]).target == DEFAULT_TARGET


def make_fixture(tmp_path: Path):
    source = tmp_path / "originals" / "A"
    source.mkdir(parents=True)
    (source / "ABCDEF12-3456-7890-ABCD-EF1234567890.heic").write_bytes(b"photo")
    (source / "ABCDEF12-3456-7890-ABCD-EF1234567890_3.mov").write_bytes(b"motion")
    (source / "ABCDEF12-3456-7890-ABCD-EF1234567890_5.aae").write_bytes(b"edit")
    database = tmp_path / "Photos.sqlite"
    with sqlite3.connect(database) as db:
        db.executescript("""
            CREATE TABLE ZASSET (Z_PK INTEGER PRIMARY KEY, ZUUID TEXT,
              ZFILENAME TEXT, ZDATECREATED REAL, ZTRASHEDSTATE INTEGER DEFAULT 0);
            CREATE TABLE ZADDITIONALASSETATTRIBUTES (ZASSET INTEGER,
              ZORIGINALFILENAME TEXT);
        """)
        db.execute("INSERT INTO ZASSET (Z_PK, ZUUID, ZFILENAME, ZDATECREATED) VALUES (1, ?, ?, ?)",
                   ("ABCDEF12-3456-7890-ABCD-EF1234567890",
                    "ABCDEF12-3456-7890-ABCD-EF1234567890.heic", 0))
        db.execute("INSERT INTO ZADDITIONALASSETATTRIBUTES VALUES (1, ?)", ("IMG_0001.HEIC",))
        db.commit()
    return source.parent, database


def test_load_and_sync(tmp_path):
    source, database = make_fixture(tmp_path)
    assets, issues = load_assets(database, source)
    assert not issues
    assert {a.destination_name for a in assets} == {"IMG_0001.HEIC", "IMG_0001.mov"}
    result = sync(assets, tmp_path / "archive", True)
    assert result["counts"]["copied"] == 2
    assert (tmp_path / "archive/2001/01/01/IMG_0001.HEIC").read_bytes() == b"photo"
    assert (tmp_path / "archive/2001/01/01/IMG_0001.mov").read_bytes() == b"motion"


def test_zero_limit_returns_no_assets(tmp_path):
    source, database = make_fixture(tmp_path)
    assets, issues = load_assets(database, source, limit=0)
    assert not assets
    assert not issues


def test_after_filter_uses_source_modified_time(tmp_path):
    source, database = make_fixture(tmp_path)
    cutoff = time.time() + 60
    for path in source.rglob("*"):
        if path.is_file():
            os.utime(path, (cutoff + 1, cutoff + 1))

    assets, issues = load_assets(database, source, after=cutoff)

    assert not issues
    assert {asset.destination_name for asset in assets} == {"IMG_0001.HEIC", "IMG_0001.mov"}
    assert not load_assets(database, source, after=cutoff + 2)[0]


def test_after_filter_accepts_source_creation_time():
    assert source_is_created_or_modified_after(SimpleNamespace(st_mtime=100, st_birthtime=200), 150)
    assert not source_is_created_or_modified_after(SimpleNamespace(st_mtime=100), 150)


def test_after_argument_parses_iso_datetime_and_rejects_invalid_value():
    assert parse_args(["--after", "2024-01-01"]).after == datetime(2024, 1, 1).timestamp()

    with pytest.raises(SystemExit):
        parse_args(["--after", "not-a-date"])


def test_capture_date_range_includes_august_and_september_only(tmp_path):
    source, database = make_fixture(tmp_path)
    dates = [
        ("11111111-1111-1111-1111-111111111111", datetime(2026, 7, 31, 23, 59)),
        ("22222222-2222-2222-2222-222222222222", datetime(2026, 8, 1)),
        ("33333333-3333-3333-3333-333333333333", datetime(2026, 9, 30, 23, 59)),
        ("44444444-4444-4444-4444-444444444444", datetime(2026, 10, 1)),
    ]
    with sqlite3.connect(database) as db:
        for key, (uuid, captured) in enumerate(dates, start=2):
            (source / "A" / f"{uuid}.jpg").write_bytes(uuid.encode())
            db.execute("INSERT INTO ZASSET (Z_PK, ZUUID, ZFILENAME, ZDATECREATED) VALUES (?, ?, ?, ?)",
                       (key, uuid, f"IMG_{key}.JPG", captured.timestamp() - 978307200))

    assets, issues = load_assets(database, source, captured_from=date(2026, 8, 1),
                                 captured_before=date(2026, 10, 1))

    assert not issues
    assert {asset.uuid for asset in assets} == {dates[1][0], dates[2][0]}
    assert parse_args(["--captured-from", "2026-08-01",
                       "--captured-before", "2026-10-01"]).captured_from == date(2026, 8, 1)
    with pytest.raises(SystemExit):
        parse_args(["--captured-from", "not-a-date"])


def test_identical_rerun_skips(tmp_path):
    source, database = make_fixture(tmp_path)
    assets, _ = load_assets(database, source)
    target = tmp_path / "archive"
    sync(assets, target, True)
    result = sync(assets, target, True)
    assert result["counts"]["skipped"] == 2


def test_duplicate_uuid_reported_as_issue(tmp_path):
    source, database = make_fixture(tmp_path)
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO ZASSET (Z_PK, ZUUID, ZFILENAME, ZDATECREATED) VALUES (2, ?, ?, ?)",
                   ("ABCDEF12-3456-7890-ABCD-EF1234567890",
                    "IMG_9999.HEIC", 1000))
        db.commit()
    assets, issues = load_assets(database, source)
    assert any("duplicate ZUUID" in issue for issue in issues)
    # first DB row wins; original metadata is preserved
    assert {a.destination_name for a in assets} == {"IMG_0001.HEIC", "IMG_0001.mov"}


def test_invalid_original_filename_is_reported_and_skipped(tmp_path):
    source, database = make_fixture(tmp_path)
    with sqlite3.connect(database) as db:
        db.execute("UPDATE ZADDITIONALASSETATTRIBUTES SET ZORIGINALFILENAME = ?", (".",))
        db.commit()

    assets, issues = load_assets(database, source)

    assert not assets
    assert any("invalid original filename" in issue for issue in issues)


def test_new_copy_is_untouched_bytes_plus_sidecar(tmp_path):
    """New assets: media is a plain byte-for-byte copy; a sidecar is written
    alongside it using current Photos metadata. Live Photo video companions
    (part is not None) never get their own sidecar, matching real osxphotos
    --sidecar XMP, which only ever sidecars the still image half of the pair."""
    source, database = make_fixture(tmp_path)
    assets, _ = load_assets(database, source)
    target = tmp_path / "archive"
    renderer = StubSidecarRenderer()
    result = sync(assets, target, True, sidecar_renderer=renderer)
    assert result["counts"]["copied"] == 2
    assert result["counts"]["sidecar_written"] == 1
    heic = target / "2001/01/01/IMG_0001.HEIC"
    mov = target / "2001/01/01/IMG_0001.mov"
    assert heic.read_bytes() == b"photo"  # untouched, byte-for-byte
    assert sidecar_path(heic).exists()
    assert "ABCDEF12-3456-7890-ABCD-EF1234567890" in sidecar_path(heic).read_text()
    assert not sidecar_path(mov).exists()


def test_rerun_skips_media_and_unchanged_sidecar(tmp_path):
    source, database = make_fixture(tmp_path)
    assets, _ = load_assets(database, source)
    target = tmp_path / "archive"
    sync(assets, target, True, sidecar_renderer=StubSidecarRenderer())
    result = sync(assets, target, True, sidecar_renderer=StubSidecarRenderer())
    assert result["counts"]["skipped"] == 2
    assert result["counts"]["sidecar_skipped"] == 1
    assert result["counts"].get("sidecar_written", 0) == 0


def test_sidecar_rewritten_when_photos_metadata_changes(tmp_path):
    """Sidecars are fully owned/regenerated by us: if Photos metadata changes
    between runs, the sidecar is rewritten even though the media file (never
    touched) still matches the raw source."""
    source, database = make_fixture(tmp_path)
    assets, _ = load_assets(database, source)
    target = tmp_path / "archive"
    sync(assets, target, True, sidecar_renderer=StubSidecarRenderer(default="v1"))
    result = sync(assets, target, True, sidecar_renderer=StubSidecarRenderer(default="v2"))
    assert result["counts"]["skipped"] == 2  # media untouched
    assert result["counts"]["sidecar_written"] == 1
    heic = target / "2001/01/01/IMG_0001.HEIC"
    assert "v2" in sidecar_path(heic).read_text()


def test_adds_sidecar_to_previously_plain_copy(tmp_path):
    """A destination copied before sidecars existed (no sidecar_renderer used)
    just gets a sidecar added on the next run; media stays untouched."""
    source, database = make_fixture(tmp_path)
    assets, _ = load_assets(database, source)
    target = tmp_path / "archive"
    sync(assets, target, True)  # no sidecar renderer this run
    result = sync(assets, target, True, sidecar_renderer=StubSidecarRenderer())
    assert result["counts"]["skipped"] == 2
    assert result["counts"]["sidecar_written"] == 1
    heic = target / "2001/01/01/IMG_0001.HEIC"
    assert heic.read_bytes() == b"photo"


def test_legacy_media_trusted_when_sidecar_already_present(tmp_path):
    """Destination bytes differ from the raw source (e.g. modified in place by
    a prior real `osxphotos export --exiftool` run), but a sidecar already
    sits next to it: treated as legacy, not a conflict."""
    source, database = make_fixture(tmp_path)
    assets, _ = load_assets(database, source)
    target = tmp_path / "archive"
    heic = target / "2001/01/01/IMG_0001.HEIC"
    heic.parent.mkdir(parents=True)
    heic.write_bytes(b"photo+legacy-exif-bytes")
    sidecar_path(heic).write_text("<xmp>legacy</xmp>")
    mov = target / "2001/01/01/IMG_0001.mov"
    mov.write_bytes(b"motion")

    result = sync(assets, target, True, sidecar_renderer=StubSidecarRenderer())
    assert result["counts"]["legacy"] == 1
    assert result["counts"].get("conflict", 0) == 0
    # sidecar for the legacy file is still refreshed with current metadata
    assert "v1" in sidecar_path(heic).read_text()


def test_reports_genuine_conflict_without_sidecar(tmp_path):
    """Destination bytes differ from the raw source and no sidecar exists:
    genuine conflict, not touched."""
    source, database = make_fixture(tmp_path)
    assets, _ = load_assets(database, source)
    target = tmp_path / "archive"
    heic = target / "2001/01/01/IMG_0001.HEIC"
    heic.parent.mkdir(parents=True)
    heic.write_bytes(b"totally unrelated content")
    mov = target / "2001/01/01/IMG_0001.mov"
    mov.write_bytes(b"motion")

    result = sync(assets, target, True, sidecar_renderer=StubSidecarRenderer())
    assert result["counts"]["conflict"] == 1
    assert result["counts"]["skipped"] == 1
    # the conflicting file's sidecar is left untouched
    assert not sidecar_path(heic).exists()


def test_symlinked_destination_is_reported_as_a_conflict(tmp_path):
    source, database = make_fixture(tmp_path)
    assets, _ = load_assets(database, source)
    target = tmp_path / "archive"
    destination = target / "2001/01/01/IMG_0001.HEIC"
    destination.parent.mkdir(parents=True)
    outside_file = tmp_path / "outside.heic"
    outside_file.write_bytes(b"outside")
    destination.symlink_to(outside_file)

    result = sync(assets, target, True)

    assert result["counts"]["conflict"] == 1
    assert outside_file.read_bytes() == b"outside"


def test_sync_rejects_non_positive_progress_interval(tmp_path):
    asset = Asset("uuid", tmp_path / "source.heic", "IMG_0001.HEIC", datetime(2020, 1, 1))

    with pytest.raises(ValueError, match="progress_every must be positive"):
        sync([asset], tmp_path / "archive", False, progress_every=0)


def test_sync_processes_assets_by_capture_timestamp(tmp_path):
    renderer = StubSidecarRenderer()
    assets = [
        Asset("newer", tmp_path / "newer.heic", "newer.HEIC", datetime(2020, 1, 2)),
        Asset("same-b", tmp_path / "same-b.heic", "same-b.HEIC", datetime(2020, 1, 1)),
        Asset("same-a", tmp_path / "same-a.heic", "same-a.HEIC", datetime(2020, 1, 1)),
        Asset("older", tmp_path / "older.heic", "older.HEIC", datetime(2019, 12, 31)),
    ]

    sync(assets, tmp_path / "archive", False, sidecar_renderer=renderer)

    assert renderer.rendered == ["older", "same-a", "same-b", "newer"]


def test_live_photo_video_companion_trusted_via_sibling_image_sidecar(tmp_path):
    """Real-world pattern confirmed against an actual archive: a Live Photo's
    `.mov` video companion never gets its own sidecar from real osxphotos
    `--sidecar XMP` (only the still image half of the pair does), yet its
    bytes were still modified in place by a prior real `--exiftool` run. It
    must be trusted as legacy via the *sibling* image's sidecar, not its own
    (which never existed and never will)."""
    source, database = make_fixture(tmp_path)
    assets, _ = load_assets(database, source)
    target = tmp_path / "archive"
    heic = target / "2001/01/01/IMG_0001.HEIC"
    heic.parent.mkdir(parents=True)
    heic.write_bytes(b"photo")  # matches raw source: normal, already-synced image
    sidecar_path(heic).write_text("<xmp>image sidecar</xmp>")
    mov = target / "2001/01/01/IMG_0001.mov"
    mov.write_bytes(b"motion+legacy-exif-bytes")  # differs from raw source; no sidecar of its own

    assert has_sibling_sidecar(mov)
    result = sync(assets, target, True, sidecar_renderer=StubSidecarRenderer())
    assert result["counts"]["legacy"] == 1
    assert result["counts"].get("conflict", 0) == 0
    # the video companion still gets no sidecar of its own
    assert not sidecar_path(mov).exists()


def test_trashed_assets_are_excluded_without_issues(tmp_path):
    """Items in Recently Deleted still have originals on disk; they must be
    skipped without being misreported as missing a database record."""
    source, database = make_fixture(tmp_path)
    with sqlite3.connect(database) as db:
        db.execute("UPDATE ZASSET SET ZTRASHEDSTATE = 1")

    assets, issues = load_assets(database, source)

    assert assets == []
    assert issues == []


def test_trashed_duplicate_does_not_conflict_with_live_asset(tmp_path):
    source, database = make_fixture(tmp_path)
    trashed_uuid = "11111111-1111-1111-1111-111111111111"
    (source / "A" / f"{trashed_uuid}.heic").write_bytes(b"photo")
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO ZASSET (Z_PK, ZUUID, ZFILENAME, ZDATECREATED, ZTRASHEDSTATE) VALUES (2, ?, ?, 0, 1)",
                   (trashed_uuid, "IMG_0001.HEIC"))

    assets, issues = load_assets(database, source)
    result = sync(assets, tmp_path / "archive", True)

    assert not issues
    assert trashed_uuid not in {asset.uuid for asset in assets}
    assert result["counts"]["conflict"] == 0
    assert (tmp_path / "archive/2001/01/01/IMG_0001.HEIC").read_bytes() == b"photo"


APPLEDOUBLE = b"\x00\x05\x16\x07" + b"\x00" * 60


def seed_appledouble(target: Path) -> dict[str, Path]:
    """AppleDouble files at each level, plus files the cleanup must not touch."""
    day = target / "2001/01/01"
    day.mkdir(parents=True)
    other_day = target / "1999/12/31"
    other_day.mkdir(parents=True)
    paths = {
        "day": day / "._IMG_0001.HEIC",
        "day_directory": day.parent / "._01",
        "root": target / "._2001",
        "real_file": day / "._real.txt",
        "untouched_directory": other_day / "._IMG_9999.HEIC",
    }
    for key, path in paths.items():
        path.write_bytes(b"not appledouble" if key == "real_file" else APPLEDOUBLE)
    return paths


def test_apply_removes_appledouble_files_only_where_it_wrote(tmp_path):
    source, database = make_fixture(tmp_path)
    assets, _ = load_assets(database, source)
    target = tmp_path / "archive"
    paths = seed_appledouble(target)

    result = sync(assets, target, True, sidecar_renderer=StubSidecarRenderer())

    assert result["counts"]["appledouble_removed"] == 3
    for key in ("day", "day_directory", "root"):
        assert not paths[key].exists()
    assert paths["real_file"].read_bytes() == b"not appledouble"
    assert paths["untouched_directory"].exists()
    assert (target / "2001/01/01/IMG_0001.HEIC").read_bytes() == b"photo"


def test_dry_run_leaves_appledouble_files(tmp_path):
    source, database = make_fixture(tmp_path)
    assets, _ = load_assets(database, source)
    target = tmp_path / "archive"
    paths = seed_appledouble(target)

    result = sync(assets, target, False)

    assert result["counts"]["appledouble_removed"] == 0
    assert all(path.exists() for path in paths.values())
