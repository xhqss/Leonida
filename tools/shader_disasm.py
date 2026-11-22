"""Disassemble the native RDNA2 code in unpacked FSSM shader blobs with LLVM's AMDGPU disassembler.

FSSM header (little-endian qwords at 0x00..): 'FSSM' 01 01, blob size, ..., then a table where the
code range appears as (offset, size) = qword[6], qword[7] in every blob checked so far (e.g. 0x2f8, 0x100).
The code ends with s_endpgm (0xBF810000). If the header guess doesn't hold for a blob, the tool falls back to
scanning back from the first s_endpgm to the start that decodes with no invalid instructions.

Usage:
    python -I shader_disasm.py <fssm dir or file> [--out DIR] [--mcpu gfx1013] [--limit N]
Writes <out>/<name>.s and prints a summary (decoded cleanly / with invalid words / failed).
Set LLVM_MC to llvm-mc's path, or to a whole command such as
'wsl -d Ubuntu -e /usr/lib/llvm-21/bin/llvm-mc' when the tool is not on PATH.
"""
import argparse
import os
import struct
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mc_command import mc_command  # noqa: E402

TRIPLE = "amdgcn--amdpal"
ENDPGM = b"\x00\x00\x81\xbf"
PROBE = "0x00 0x00 0x81 0xbf"  # s_endpgm: the one instruction every shader ends with
FAILED = []                    # (path, first stderr line) for shaders that produced no disassembly


MC = mc_command()


def fail(msg: str) -> "None":
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def positive_int(text: str) -> int:
    """--jobs 0 made ThreadPoolExecutor raise, and --limit -1 silently dropped the last file."""
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid int value: {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def probe(mcpu: str) -> None:
    """Disassemble one s_endpgm first, so a missing tool or AMDGPU target fails now and not per shader."""
    try:
        p = subprocess.run(MC + ["--disassemble", f"-triple={TRIPLE}", f"-mcpu={mcpu}"],
                           input=PROBE, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           creationflags=getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0))
    except OSError as e:
        fail(f"'{MC[0]}' could not be started: {e}. Put llvm-mc on PATH or set LLVM_MC to it (on Windows,\n"
             f"scripts\\build_llvm_mc.ps1 builds one and sets it), e.g.\n"
             f"  LLVM_MC=wsl -d Ubuntu -e /usr/lib/llvm-21/bin/llvm-mc")
    if p.returncode != 0 or "s_endpgm" not in p.stdout:
        why = (p.stderr.strip().splitlines() or [f"exit code {p.returncode}"])[0]
        fail(f"'{' '.join(MC)}' did not disassemble an s_endpgm: {why}\n"
             f"The disassembler needs LLVM 21+ with the AMDGPU target. The official Windows LLVM build has no\n"
             f"AMDGPU target: build a native one with scripts\\build_llvm_mc.ps1, or run it from WSL\n"
             f"(docs/design.md, 'Disassembling shaders'), e.g.\n"
             f"  LLVM_MC=wsl -d Ubuntu -e /usr/lib/llvm-21/bin/llvm-mc")


def disasm(code: bytes, mcpu: str):
    text = " ".join(f"0x{b:02x}" for b in code)
    try:
        p = subprocess.run(MC + ["--disassemble", f"-triple={TRIPLE}", f"-mcpu={mcpu}", "-show-encoding"],
                           input=text, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           creationflags=getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0))
    except OSError as e:
        return "", 0, str(e)
    bad = p.stderr.count("invalid instruction encoding")
    if p.returncode != 0:
        return "", bad, p.stderr.strip() or f"llvm-mc exited with code {p.returncode}"
    return p.stdout, bad, p.stderr


def code_range(b: bytes):
    """The code section offset is the u32 at 0xC0; instructions start 0x28 bytes into it and run to the last
    4-byte-aligned s_endpgm in the blob (some blobs hold several programs). Found by correlating against
    clean decodes of 154 random shaders."""
    # A blob too short to hold the offset goes to the scan like any other header miss; reading it anyway raised
    # struct.error, which stopped the whole run (and make_game.py at the same blob on every rerun).
    off = struct.unpack_from("<I", b, 0xC0)[0] if len(b) >= 0xC4 else 0
    start = off + 0x28
    if 0 < off and start < len(b):
        last = b.rfind(ENDPGM, start)
        while last >= 0 and (last - start) % 4:
            last = b.rfind(ENDPGM, start, last)
        if last >= 0:
            return start, last + 4, "header"
    end = b.find(ENDPGM)
    return None, end + 4 if end >= 0 else None, "scan"


def one(path, out, mcpu):
    with open(path, "rb") as f:
        b = f.read()
    start, end, how = code_range(b)
    if end is None:
        return "no s_endpgm"
    if start is None:
        # Walk back in 4-byte steps from the end and keep the longest clean decode.
        start, best = end - 4, None
        for s in range(end - 4, max(0x100, end - 0x4000) - 1, -4):
            _, bad, _ = disasm(b[s:end], mcpu)
            if bad == 0:
                best = s
        if best is None:
            return "scan failed"
        start = best
    asm, bad, err = disasm(b[start:end], mcpu)
    if not asm.strip():  # a broken toolchain, not a shader: never report this as "clean"
        reason = (err.strip().splitlines() or ["no output"])[0]
        FAILED.append((path, reason))
        return "failed"
    name = os.path.splitext(os.path.basename(path))[0]
    with open(os.path.join(out, name + ".s"), "w", encoding="utf-8") as f:
        f.write(f"; {name}  code 0x{start:x}..0x{end:x} ({how}), {mcpu}, {bad} invalid words\n")
        f.write(asm)
        if err.strip():
            f.write("\n; --- llvm-mc messages ---\n; " + err.strip().replace("\n", "\n; ") + "\n")
    return "clean" if bad == 0 else "invalid words"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("--out")
    ap.add_argument("--mcpu", default="gfx1013")
    ap.add_argument("--limit", type=positive_int)
    ap.add_argument("--jobs", type=positive_int, default=max(1, (os.cpu_count() or 2) // 2))
    a = ap.parse_args()
    if os.path.isfile(a.src):
        files = [a.src]
    elif os.path.isdir(a.src):
        with os.scandir(a.src) as entries:
            files = sorted(entry.path for entry in entries
                           if entry.name.endswith(".fssm") and entry.is_file())
    else:
        fail(f"{a.src} is not a file or folder")
    if not files:
        fail(f"no .fssm files in {a.src}")
    files = files[: a.limit] if a.limit else files
    out = a.out or os.path.join(os.path.dirname(files[0]), "..", "disasm")
    try:
        os.makedirs(out, exist_ok=True)
    except OSError as e:
        fail(f"cannot create the output folder {out}: {e.strerror or e}")
    probe(a.mcpu)
    from collections import Counter
    with ThreadPoolExecutor(a.jobs) as ex:
        res = Counter(ex.map(lambda p: one(p, out, a.mcpu), files))
    print(f"{len(files)} shaders -> {os.path.abspath(out)}: {dict(res)}")
    if FAILED:
        path, reason = FAILED[0]
        fail(f"{len(FAILED)} shader(s) produced no disassembly, first {path}: {reason}")
    if not (res["clean"] or res["invalid words"]):
        fail(f"no disassembly produced for {a.src}")
    return 0


if __name__ == "__main__":
    try:  # a redirected stdout is strict ANSI code page under -I; write '?' for what it cannot encode
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError, OSError):
        pass
    sys.exit(main())
