"""Synthetic FSSM compute headers shared by both GPU test runners."""
import os
import re
import struct
from pathlib import Path


def compute_header(asm_path):
    # Default: four user SGPRs (V#), TGID_X, 512 bytes LDS, 64 threads.
    rsrc2, group = 0x8088, (64, 1, 1)
    for line in Path(asm_path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        if not line.startswith(";"):
            break
        size = re.match(r";\s*group:\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)", line)
        if size:
            group = tuple(map(int, size.groups()))
        elif line[1:].lstrip().startswith("rsrc2:"):
            rsrc2 = int(line.split(":", 1)[1].strip(), 0)
    header = bytearray(0x148)
    struct.pack_into("<4I", header, 0x138, rsrc2, *group)
    return header


def unpinned_subgroup(out):
    """The lane count vk_compute_test was asked for with VKC_SUBGROUP_SIZE and could not pin on this device, or None.

    vk_compute_test then prints "compute subgroup size: N not available" and runs at whatever size the driver picks
    (#774); Mesa llvmpipe's only size is 8. A wave32 program's words from such a run say nothing about the translator,
    so the fuzzers skip it, as check.py's unpinned_subgroup_skip does for its lane-mask dumps. Output without the line
    (the size was pinned, none was asked for, or an older vk_compute_test) gives None."""
    m = re.search(r"^compute subgroup size: (\d+) not available$", out, re.MULTILINE)
    return int(m.group(1)) if m else None


def find_test_program(build, name):
    """The folder holding test program `name`: `build` itself (Ninja, Makefiles) or the RelWithDebInfo/Release
    subfolder a Visual Studio build puts it in, as run_gpu_tests.py's find_test_programs looks. None if not built.
    A Debug configuration isn't searched (neither does run_gpu_tests.py): GPU fuzzing wants an optimized harness."""
    exe = name + (".exe" if os.name == "nt" else "")
    for folder in (build, os.path.join(build, "RelWithDebInfo"), os.path.join(build, "Release")):
        if os.path.isfile(os.path.join(folder, exe)):
            return folder
    return None
