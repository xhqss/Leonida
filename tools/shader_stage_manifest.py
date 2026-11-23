"""Read shader names from a completed carve or unpack index."""

import json
import ntpath
import posixpath


def shader_stems(path: str, field: str, suffix: str = "") -> set[str]:
    try:
        with open(path, encoding="utf-8") as f:
            rows = json.load(f)
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path}: invalid UTF-8: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(rows, list):
        raise ValueError(f"{path}: expected an array of shader entries")
    stems = set()
    for row in rows:
        value = row.get(field) if isinstance(row, dict) else None
        if (
            not isinstance(value, str)
            or not value
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or ":" in value
            or "/" in value
            or "\\" in value
            or ntpath.splitdrive(value)[0]
            or ntpath.basename(value) != value
            or posixpath.basename(value) != value
        ):
            raise ValueError(f"{path}: expected a shader basename in {field!r}")
        if suffix and (not value.endswith(suffix) or len(value) == len(suffix)):
            raise ValueError(f"{path}: expected a {suffix} filename in {field!r}")
        stem = value.removesuffix(suffix)
        if not stem or stem in (".", ".."):
            raise ValueError(f"{path}: expected a shader basename in {field!r}")
        stems.add(stem)
    return stems
