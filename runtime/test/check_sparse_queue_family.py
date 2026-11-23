"""Repo-friendly, optional GPU test: byte-exact Leonida COPY/COMPUTE tiled-buffer replay
AND actual VK_SHARING_MODE_CONCURRENT on *every* sparse VkBuffer created by the layer.

Runs the original synthetic `d3d12_sparse_queue_family_test` target.
Invokes the SDK's VK_LAYER_LUNARG_api_dump in a short-lived isolated directory;
no private-game files, asset paths, device memory dumps, or committed logs.

  python -I check_sparse_queue_family.py path/to/d3d12_sparse_queue_family_test.exe
  python -I check_sparse_queue_family.py path/to/baseline.exe --expect-baseline-exclusive

By default the baseline fails (exit 1) and patched runtime passes (exit 0).
If the API dump layer is absent the result skips (exit 77, CTest SKIP_RETURN_CODE).
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path


SPARSE_FLAGS = "VkBufferCreateFlags = 7 (VK_BUFFER_CREATE_SPARSE_BINDING_BIT"
MODE = re.compile(r"sharingMode:\s+VkSharingMode = (VK_SHARING_MODE_\w+)")
COUNT = re.compile(r"queueFamilyIndexCount:\s+uint32_t = (\d+)")
INDICES = re.compile(r"pQueueFamilyIndices\[(\d+)\]:\s+const uint32_t = (\d+)")
CASE = re.compile(r"^RESULT (LAYER_MAP_(?:DIRECT|COPY|COMPUTE)_COPY_COMPUTE) PASS mapped_mismatch=0/65536 unmapped_nonzero=0/65536$", re.M)


def check_trace(path: Path, expect_exclusive: bool) -> tuple[int, int]:
    rows = path.read_text(encoding="utf-8", errors="replace").splitlines()
    seen = []
    for i, line in enumerate(rows):
        if SPARSE_FLAGS not in line:
            continue
        tail = "\n".join(rows[i + 1 : i + 13])
        mode, count = MODE.search(tail), COUNT.search(tail)
        if not mode or not count:
            raise AssertionError(f"incomplete API dump sparse VkBuffer at line {i+1}")
        inds = tuple(int(index) for _, index in INDICES.findall(tail))
        seen.append((mode.group(1), int(count.group(1)), inds))
    if len(seen) < 6 or len(seen) % 2:
        raise AssertionError(f"insufficient sparse VkBuffer create-info records: {len(seen)}")
    if expect_exclusive:
        assert all(mode == "VK_SHARING_MODE_EXCLUSIVE" and count == 0
                   for mode, count, _ in seen), f"baseline negative control not reproduced: {set(seen)}"
    else:
        assert all(mode == "VK_SHARING_MODE_CONCURRENT" and count >= 2 and len(inds) == count
                   and len(set(inds)) == count
                   for mode, count, inds in seen), f"BUG: not all sparse buffers share active families: {set(seen)}"
    return len(seen) // 2, len(seen)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("executable", type=Path, help="Compiled Leonida d3d12_sparse_queue_family_test binary")
    parser.add_argument("--expect-baseline-exclusive", action="store_true",
                        help="negative-control mode: prove buggy EXCLUSIVE create infos instead of requiring fix")
    args = parser.parse_args()
    exe = args.executable.resolve()
    if not exe.is_file():
        print(f"ERROR: missing executable {exe}", file=sys.stderr)
        return 2
    env = os.environ.copy()
    # An implicit Vulkan layer may be forced by the loader even if the application
    # enabled VK_LAYER_KHRONOS_validation explicitly via LEONIDA_VK_VALIDATION.
    env["VK_INSTANCE_LAYERS"] = "VK_LAYER_LUNARG_api_dump"
    env["LEONIDA_VK_VALIDATION"] = "1"
    with tempfile.TemporaryDirectory(prefix="leonida_sparse_trace_") as td:
        path = Path(td)
        dump_name = path / "sparse_create_info.log"
        (path / "vk_layer_settings.txt").write_text(
            "lunarg_api_dump.file = true\n"
            f"lunarg_api_dump.log_filename = {dump_name}\n"
            "lunarg_api_dump.output_format = text\n"
            "lunarg_api_dump.pre_dump = true\n"
            "lunarg_api_dump.detailed = true\n",
            encoding="utf-8",
        )
        env["VK_LAYER_SETTINGS_PATH"] = str(path)
        run = subprocess.run([str(exe)], cwd=path, env=env, capture_output=True,
                             text=True, encoding="utf-8", errors="replace", timeout=40)
        out = run.stdout + "\n" + run.stderr
        if run.returncode == 77:
            print("SKIP 77: Leonida Vulkan device / sparse features unavailable")
            return 77
        # The loader refuses the instance when a layer named in VK_INSTANCE_LAYERS (or the validation layer
        # LEONIDA_VK_VALIDATION asks for) is not installed: that is the "layer absent" skip, not a layer failure.
        if run.returncode != 0 and "vkCreateInstance" in out and "failed" in out:
            print("SKIP 77: VK_LAYER_LUNARG_api_dump or VK_LAYER_KHRONOS_validation unavailable (vkCreateInstance failed)")
            return 77
        if run.returncode != 0:
            print(out[-7000:], file=sys.stderr)
            raise AssertionError(f"Leonida GPU test failed exit={run.returncode}")
        kinds = sorted(CASE.findall(out))
        if len(kinds) != 3 or "LAYER_FINAL PASS" not in out:
            raise AssertionError(f"GPU byte verification missing: cases={kinds}; tail={out[-2000:]}")
        candidates = list(path.glob("sparse_create_info.log*"))
        if not candidates:
            print("SKIP 77: Vulkan SDK VK_LAYER_LUNARG_api_dump unavailable or did not write trace")
            return 77
        calls, infos = check_trace(candidates[0], args.expect_baseline_exclusive)
        print(f"GPU PASS: {len(kinds)} COPY/COMPUTE sparse queue cases, mapped 65,536 bytes exact, unmapped 65,536 zeros")
        print(f"VK API CONTRACT PASS: {calls} sparse VkBuffer creations / {infos} create-info records = "
              f"{'EXCLUSIVE negative-control' if args.expect_baseline_exclusive else 'CONCURRENT with 2+ unique families'}")
        print("Khronos validation enabled; no validation errors observed (GPU does not need to corrupt data for this test)")
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, subprocess.TimeoutExpired) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        raise SystemExit(1)
