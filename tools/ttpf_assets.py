"""Complete the rpf9dump texture-pool manifest during the assets build."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import zlib

# `python -I` implies `-P`, so the script's directory is not on sys.path; the sibling modules are next to it.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bcpack
from file_mode import published_mode
import ttpf_mips as ttpf


def load_profile(work):
    path = Path(work, "bcpack-calibration.json")
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return "v1-strict", None
    profile = json.loads(raw)
    if not isinstance(profile, dict) or profile.get("version") != 1:
        raise ValueError(f"Unsupported calibration version in {path}")
    try:
        from calibrated_texture import native
        native()
    except (OSError, ImportError) as exc:
        raise RuntimeError("Calibrated texture export requires NumPy and the release build of "
                           "tools/bcpack_calibration (see its README.md)") from exc
    return "v1-" + hashlib.sha256(raw).hexdigest(), profile


def publish(path, data):
    f = tempfile.NamedTemporaryFile(dir=path.parent, delete=False)
    tmp = Path(f.name)
    try:
        with f:
            f.write(data)
        if os.name != "nt":
            tmp.chmod(published_mode(path))
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def candidate(row, profile):
    if row.get("DxgiFormat") not in ttpf.BC_FORMATS:
        return False
    if row.get("BCPackDecoder"):
        return profile is not None or row["BCPackDecoder"].endswith("-strict")
    return (row["File"] is None or row.get("BaseMipLevel", 0) > 0
            or row.get("Mips", 0) < row.get("SourceMips", 0))


def complete_pool(source, directory, manifest, settings):
    """Publish full chains next to ordinary DDS files, then switch the catalog to them."""
    stamp, profile = settings
    if manifest.get("BCPackPass") == stamp:
        return 0, 0
    rows = [row for row in manifest["Textures"] if candidate(row, profile)]
    if not rows:
        return 0, 0
    directory = Path(directory)
    written = blocked = 0
    with closing(ttpf.Pool(source)) as pool:
        decoder = None
        if profile is not None:
            from calibrated_texture import LocalDecoder
            decoder = LocalDecoder(pool, profile)
        try:
            for row in rows:
                index = row["Index"]
                desc = pool.descriptor(index)
                base = desc["base"]
                if not pool.chunks or base < pool.chunks[0][0] or base >= pool.virtual_size:
                    continue
                span = pool.span(index, base)
                if not span:
                    continue
                try:
                    if not pool.codecs(base, span) & bcpack.SUPPORTED_REBUILD_CODECS:
                        continue
                    if decoder and desc["dimension"] == 1 and desc["format"] in decoder.formats:
                        method = stamp
                        blob = decoder.dds(index)
                    else:
                        method = "v1-strict"
                        analysis = ttpf.analyze(pool, index)
                        blob = ttpf.texture_dds(pool, analysis)
                except (ValueError, KeyError, IndexError, ttpf.BcpackRequired,
                        bcpack.BcpackUnsupportedCodec, zlib.error) as exc:
                    row["BCPackLimitation"] = str(exc)
                    blocked += 1
                    continue
                # A distinct name keeps an existing lower-mip DDS usable until the manifest switches.
                name = f"{index:06d}_bcpack_{method[:19]}.dds"
                publish(directory / name, blob)
                row.update(File=name, Mips=desc["mips"], BaseMipLevel=0, Limitation=None, BCPackDecoder=method)
                row.pop("BCPackLimitation", None)
                written += 1
        finally:
            if decoder:
                decoder.read_chunk.cache_clear()
    manifest["BCPackPass"] = stamp
    publish(directory / "manifest.json", json.dumps(manifest, indent=2).encode("utf-8"))
    return written, blocked
