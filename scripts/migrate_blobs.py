"""One-shot blob store migration.

Moves the content-addressed blob store to a new root (ideally on the SAME
volume as the QQNT client cache), dedups against that cache via hardlinks
(one physical copy for anything the QQ client already has), recreates the
names/ hardlink tree, and rewrites stored file paths in:

  - every account DB:        <data_dir>/<uin>/muze.db  -> media.file_path
  - the blob index:          <old_root>/index.db       -> dedup.file_path

Run with the service STOPPED:

    scripts\\start.ps1 -Stop
    .venv\\Scripts\\python.exe scripts\\migrate_blobs.py [--prune]
    scripts\\start.ps1

--prune deletes the OLD blob tree after a successful migration (default:
keep it; verify first, reclaim by hand).

Pure ASCII on purpose (PowerShell 5.1 reads no-BOM files as ANSI).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path

HEX32 = re.compile(r"(?i)[0-9a-f]{32}")
KIND_DIRS = ("image", "video", "file", "record", "face")


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.2f}{unit}"
        n /= 1024
    return f"{n:.2f}PB"


def md5_of(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def build_cache_index(qq_root: Path) -> dict[str, list[Path]]:
    """md5 tokens embedded in QQNT cache filenames -> candidate paths."""
    idx: dict[str, list[Path]] = {}
    if not qq_root.is_dir():
        return idx
    for root, _dirs, files in os.walk(qq_root):
        for fn in files:
            for tok in set(m.group(0).lower() for m in HEX32.finditer(fn)):
                idx.setdefault(tok, []).append(Path(root) / fn)
    return idx


def migrate_blob(src: Path, dst: Path, cache_idx: dict[str, list[Path]]) -> str:
    """Returns 'skip' (already there), 'hardlink' (deduped vs QQ cache),
    or 'copy'. A cache candidate counts only with the same md5 embedded in
    its filename AND the same size."""
    if dst.exists():
        return "skip"
    dst.parent.mkdir(parents=True, exist_ok=True)
    size = src.stat().st_size
    digest = md5_of(src)
    for cand in cache_idx.get(digest, ()):
        try:
            if cand.stat().st_size == size:
                os.link(cand, dst)
                return "hardlink"
        except OSError:
            continue
    shutil.copy2(src, dst)
    return "copy"


def migrate_blobs(old: Path, new: Path, cache_idx: dict[str, list[Path]]) -> dict:
    stats = {"copy": 0, "hardlink": 0, "skip": 0, "bytes": 0, "saved": 0}
    inode_map: dict[tuple[int, int], Path] = {}  # old blob inode -> new path
    t0 = time.monotonic()
    total = 0
    for kind in KIND_DIRS:
        kdir = old / kind
        if not kdir.is_dir():
            continue
        files = [p for p in kdir.rglob("*") if p.is_file()]
        total += len(files)
        for i, src in enumerate(files, 1):
            rel = src.relative_to(old)
            dst = new / rel
            how = migrate_blob(src, dst, cache_idx)
            stats[how] += 1
            stats["bytes"] += src.stat().st_size
            if how == "hardlink":
                stats["saved"] += src.stat().st_size
            try:
                st = src.stat()
                inode_map[(st.st_ino, st.st_dev)] = dst
            except OSError:
                pass
            if i % 500 == 0:
                pct = i / max(1, len(files)) * 100
                print(f"  {kind}: {i}/{len(files)} ({pct:.0f}%) "
                      f"elapsed {time.monotonic() - t0:.0f}s", flush=True)
    # names/: hardlinks to content blobs; relink against the new tree
    names_old, names_new = old / "names", new / "names"
    relinked = copied = 0
    if names_old.is_dir():
        names_new.mkdir(parents=True, exist_ok=True)
        for nm in names_old.iterdir():
            if not nm.is_file():
                continue
            target = names_new / nm.name
            if target.exists():
                continue
            try:
                st = nm.stat()
                blob_new = inode_map.get((st.st_ino, st.st_dev))
            except OSError:
                blob_new = None
            if blob_new is not None and blob_new.exists():
                try:
                    os.link(blob_new, target)
                    relinked += 1
                    continue
                except OSError:
                    pass
            shutil.copy2(nm, target)
            copied += 1
    stats["names_relinked"] = relinked
    stats["names_copied"] = copied
    stats["files"] = total
    return stats


def rewrite_index_db(old: Path, new: Path) -> int:
    src_db = old / "index.db"
    if not src_db.is_file():
        return 0
    new.parent.mkdir(parents=True, exist_ok=True)
    dst_db = new / "index.db"
    shutil.copy2(src_db, dst_db)
    for suffix in ("-wal", "-shm"):
        side = Path(str(src_db) + suffix)
        if side.is_file():
            shutil.copy2(side, Path(str(dst_db) + suffix))
    conn = sqlite3.connect(str(dst_db))
    try:
        cur = conn.execute(
            "UPDATE dedup SET file_path = ? || substr(file_path, ?) "
            "WHERE file_path LIKE ?",
            (str(new) + "\\", len(str(old)) + 2, str(old) + "%"),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def rewrite_account_dbs(data_dir: Path, old: Path, new: Path) -> dict[str, int]:
    out: dict[str, int] = {}
    for db in sorted(data_dir.glob("*/muze.db")):
        conn = sqlite3.connect(str(db))
        try:
            cur = conn.execute(
                "UPDATE media SET file_path = ? || substr(file_path, ?) "
                "WHERE file_path LIKE ?",
                (str(new) + "\\", len(str(old)) + 2, str(old) + "%"),
            )
            conn.commit()
            out[db.parent.name] = cur.rowcount
        finally:
            conn.close()
    return out


def main(argv: list[str] | None = None) -> int:
    here = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--from", dest="old", default=str(here / "data" / "blobs"))
    ap.add_argument("--to", dest="new", default=r"D:\Data\Copilot_Muze")
    ap.add_argument("--qq-cache", default=r"D:\Data\Tencent Files\nt_qq\global\nt_data")
    ap.add_argument("--data-dir", default=str(here / "data"))
    ap.add_argument("--prune", action="store_true",
                    help="delete the OLD blob tree after a verified migration")
    args = ap.parse_args(argv)

    old, new = Path(args.old).resolve(), Path(args.new).resolve()
    qq_root, data_dir = Path(args.qq_cache), Path(args.data_dir).resolve()

    if not old.is_dir():
        print(f"old blob root not found: {old}")
        return 2
    if old == new:
        print("old and new roots are the same; nothing to do")
        return 2
    new.mkdir(parents=True, exist_ok=True)

    free = shutil.disk_usage(new).free
    print(f"migrating blobs:\n  {old} ->\n  {new}\n  qq cache: {qq_root}")
    print(f"  new volume free space: {human(free)}")

    print("indexing QQNT cache filenames ...", flush=True)
    cache_idx = build_cache_index(qq_root)
    print(f"  {sum(len(v) for v in cache_idx.values())} md5 tokens "
          f"across {len(cache_idx)} entries")

    print("migrating blobs (copy / hardlink-dedup) ...", flush=True)
    stats = migrate_blobs(old, new, cache_idx)
    print(f"  files={stats['files']} copied={stats['copy']} "
          f"deduped(hardlink)={stats['hardlink']} skipped={stats['skip']}")
    print(f"  moved {human(stats['bytes'])}, dedup saved {human(stats['saved'])}")
    print(f"  names/: {stats['names_relinked']} relinked, "
          f"{stats['names_copied']} copied")

    n = rewrite_index_db(old, new)
    print(f"index.db: copied, {n} dedup rows rewritten")
    for acct, rows in rewrite_account_dbs(data_dir, old, new).items():
        print(f"account {acct}: {rows} media rows rewritten")

    if args.prune:
        print(f"pruning old tree {old} ...")
        for child in old.iterdir():
            if child.name == "tmp":
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink(missing_ok=True)
        print("old tree pruned (tmp/ left in place)")
    else:
        print(f"old tree KEPT at {old}; verify, then rerun with --prune "
              f"(or delete by hand) to reclaim {human(stats['bytes'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
