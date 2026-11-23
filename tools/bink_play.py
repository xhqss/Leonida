"""Play a game movie by name, straight from its archive, with runtime/test/bink_play.

The movie is never extracted: this finds its offset in ``movies.rpf`` with ``bink_names.py`` (from the mount lists in
``common.rpf``) and starts ``bink_play <archive>@<offset>``, which hands the archive to the package's own
Bink2Scarlett.dll with ``BinkSetFileOffset``.

Usage:
    python tools/bink_play.py Stinger/RSG_STINGER.bk2 [--game-dir DIR] [--exe bink_play.exe] [-- bink_play args]
    python tools/bink_play.py stinger          # any unique part of the name, case-insensitive
    python tools/bink_play.py --list [TEXT]    # the names (and sizes) it knows, optionally filtered

``--game-dir`` defaults to ``LEONIDA_GAME_DIR``. Naming the streams takes about half a minute, so the name table
(names, offsets and frame sizes only, no movie data) is kept in ``%LOCALAPPDATA%\\Leonida\\bink_names.csv`` and rebuilt
when the archive changes; ``--no-cache`` skips it. The names are a local work product: don't commit or post them.
"""
import argparse
import csv
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import bink_names  # noqa: E402


def cache_path():
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".cache")
    return Path(base) / "Leonida" / "bink_names.csv"


def stamp(archive):
    st = archive.stat()
    return f"{archive.resolve()}|{st.st_size}|{st.st_mtime_ns}"


def load_names(archive, mount, use_cache=True):
    """[(path, offset, width, height, frames)] for the streams of ``archive``, named from ``mount``."""
    cache = cache_path()
    key = stamp(archive)
    if use_cache and cache.is_file():
        with cache.open(encoding="utf-8", newline="") as f:
            first = f.readline().rstrip("\r\n")
            if first == "# " + key:
                return [(r["path"], int(r["offset"]), r["width"], r["height"], r["frames"]) for r in csv.DictReader(f)]
    tmp = cache.with_suffix(".tmp.csv")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    print(f"naming the movies in {archive.name} (about half a minute, once)...", file=sys.stderr)
    rc = bink_names.main([str(archive), "--mount", str(mount), "--out", str(tmp)])
    if rc not in (0, None) or not tmp.is_file():
        raise SystemExit(f"bink_names.py failed on {archive}")
    with tmp.open(encoding="utf-8", newline="") as f:
        rows = [r for r in csv.DictReader(f) if r.get("path")]
    tmp.unlink()
    if use_cache:
        part = cache.with_suffix(".part")
        with part.open("w", encoding="utf-8", newline="") as f:
            f.write("# " + key + "\n")
            w = csv.writer(f, lineterminator="\n")
            w.writerow(["path", "offset", "width", "height", "frames"])
            for r in rows:
                w.writerow([r["path"], r["offset"], r["width"], r["height"], r["frames"]])
        os.replace(part, cache)
    return [(r["path"], int(r["offset"]), r["width"], r["height"], r["frames"]) for r in rows]


def pick(names, wanted):
    """The one entry whose path is ``wanted`` (case-insensitive), else the one that contains it."""
    w = wanted.replace("\\", "/").lower()
    exact = [n for n in names if n[0].lower() == w]
    if exact:
        return exact[0], []
    part = [n for n in names if w in n[0].lower()]
    if len(part) == 1:
        return part[0], []
    return None, part


def find_exe(explicit):
    if explicit:
        return Path(explicit)
    root = HERE.parent
    for c in (root / "runtime" / "build" / "bink_play.exe", root / "runtime" / "build" / "Release" / "bink_play.exe"):
        if c.is_file():
            return c
    raise SystemExit("bink_play.exe not found; build the runtime (runtime/build) or pass --exe")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    extra = []
    if "--" in argv:
        i = argv.index("--")
        argv, extra = argv[:i], argv[i + 1:]
    ap = argparse.ArgumentParser(description="Play a game movie by name, straight from its archive.")
    ap.add_argument("name", nargs="?", help="the movie's path (Stinger/RSG_STINGER.bk2) or a unique part of it")
    ap.add_argument("--game-dir", default=os.environ.get("LEONIDA_GAME_DIR"), help="the package folder")
    ap.add_argument("--archive", default="movies.rpf", help="the archive holding the movie (default movies.rpf)")
    ap.add_argument("--mount", default="common.rpf", help="where the mount lists are (default common.rpf)")
    ap.add_argument("--exe", help="bink_play.exe (default runtime/build/bink_play.exe)")
    ap.add_argument("--list", action="store_true", help="list the names, filtered by NAME if given")
    ap.add_argument("--no-cache", action="store_true", help="name the streams again and keep nothing")
    a = ap.parse_args(argv)
    if not a.game_dir:
        ap.error("no game folder: pass --game-dir or set LEONIDA_GAME_DIR")
    game = Path(a.game_dir)
    archive = Path(a.archive) if Path(a.archive).is_absolute() else game / a.archive
    mount = Path(a.mount) if Path(a.mount).is_absolute() else game / a.mount
    for p in (archive, mount):
        if not p.is_file():
            ap.error(f"{p} is not a file")
    names = load_names(archive, mount, use_cache=not a.no_cache)

    if a.list:
        w = (a.name or "").lower()
        for path, _, width, height, frames in names:
            if w in path.lower():
                print(f"{path}  {width}x{height}, {frames} frames")
        return 0
    if not a.name:
        ap.error("name a movie, or use --list")
    hit, candidates = pick(names, a.name)
    if hit is None:
        if not candidates:
            print(f"no movie named like {a.name!r} in {archive.name}; try --list", file=sys.stderr)
        else:
            print(f"{len(candidates)} movies match {a.name!r}; be more specific:", file=sys.stderr)
            for c in candidates[:40]:
                print("  " + c[0], file=sys.stderr)
        return 1
    path, offset, width, height, frames = hit
    print(f"{path}: {width}x{height}, {frames} frames, at {archive.name}@{offset}", flush=True)
    cmd = [str(find_exe(a.exe)), f"{archive}@{offset}", "--game-dir", str(game)] + extra
    return subprocess.call(cmd)


if __name__ == "__main__":
    try:  # a redirected stdout is strict ANSI code page under -I; write '?' for what it cannot encode
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError, OSError):
        pass
    sys.exit(main())
