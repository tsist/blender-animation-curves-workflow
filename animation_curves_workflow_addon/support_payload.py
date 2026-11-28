"""Resolve the exact reference resources in full and compact Windows packages."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tempfile
import zipfile

REFERENCE = "resources/animation_fullclip_two_segment_v2_20261114/native01/build/CHARGE-Two-Segment-v2.blend"
MANIFEST_SHA = "8d1a2f4ba0c0e7330e37a82f408af79cbd28fa38cae100180492c474a38c9c43"


def checksum(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def sample_source(package_dir):
    compact = Path(package_dir) / "CHARGE-Reference.blend"
    return compact if compact.is_file() else Path(package_dir) / "support_resources" / REFERENCE


def direct(path):
    if path != path.resolve() or any(p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction())
                                     for p in (path, *path.parents)):
        raise ValueError(f"Redirected support resource path: {path}")
    return path


def cache_path(path):
    if os.name == "nt":
        from .support_reference_windows import filesystem_path
        return filesystem_path(path)
    return path


def archive_rows(names):
    rows = {}
    seen = set()
    for name in names:
        path = PurePosixPath(name)
        if (path.is_absolute() or path.as_posix() != name or any(p in ("", ".", "..") for p in path.parts)
                or "\\" in name or ":" in name or name.casefold() in seen):
            raise ValueError(f"Invalid or duplicate support resource path: {name}")
        seen.add(name.casefold())
        rows[name] = Path(*path.parts)
    return rows


def verify_tree(root, rows, hashes):
    direct(root)
    files = set()
    for folder, dirs, names in os.walk(root):
        for sub in dirs:
            direct(Path(folder) / sub)
        files.update((Path(folder) / name).relative_to(root).as_posix() for name in names)
    if files != set(rows):
        raise ValueError(f"Support resource cache is incomplete or has extra files: {root}")
    for name, rel in rows.items():
        if checksum(direct(root / rel)) != hashes[name]:
            raise ValueError(f"Support resource changed: {root / rel}")


def bundle_root(package_dir, relative):
    if relative != "support_resources":
        raise ValueError("Unexpected support resource root")
    package_dir = direct(Path(package_dir).resolve())
    expanded = package_dir / relative
    if expanded.is_dir():
        return direct(expanded)
    archive = direct(package_dir / "support-data.zip")
    index = direct(package_dir / "support-data.json")
    release = json.loads((package_dir / "PACKAGE-MANIFEST.json").read_text(encoding="utf-8"))
    if checksum(index) != release["support-data.json"] or checksum(archive) != release["support-data.zip"]:
        raise ValueError("Installed support archive does not match its package manifest")
    hashes = json.loads(index.read_text(encoding="utf-8"))
    if hashes.get("bundle.json") != MANIFEST_SHA:
        raise ValueError("Unexpected frozen support bundle")
    rows = archive_rows(hashes)
    # The old archive exceeded MAX_PATH under the normal Blender add-on directory.
    # Keep ZIP members out of that directory and retain their module-relative structure here.
    base = Path(os.environ["LOCALAPPDATA"]) if os.name == "nt" else Path(tempfile.gettempdir()) / str(os.getuid())
    cache = direct(cache_path(base / "ACW" / "data"))
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    final = direct(cache / release["support-data.zip"][:20])
    if not final.exists():
        with tempfile.TemporaryDirectory(dir=cache, prefix="t") as temporary:
            stage = Path(temporary) / "p"
            stage.mkdir(mode=0o700)
            with zipfile.ZipFile(archive) as packed:
                members = archive_rows(packed.namelist())
                if members != rows:
                    raise ValueError("Support archive and file index differ")
                for name, rel in rows.items():
                    target = stage / rel
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    with packed.open(name) as source, target.open("xb") as output:
                        shutil.copyfileobj(source, output)
            verify_tree(stage, rows, hashes)
            try:
                stage.rename(final)
            except OSError:
                if not final.is_dir():
                    raise
    verify_tree(final, rows, hashes)
    # The worker converts individual resource paths to extended Win32 syntax.
    return base / "ACW" / "data" / final.name
