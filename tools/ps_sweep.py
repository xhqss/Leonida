"""Draw a stratified sample of translated pixel shaders on one GPU at a time and compare two GPUs.

The worker is `runtime/test/vk_ps_sweep` (build the `vk_ps_sweep` target). It draws one pixel shader against a
generated vertex shader, with the runtime's bindless set 0 holding only its fallbacks and a zeroed heap, so the
inputs are the same on every device. Run the same sample on each GPU, then compare:

    python -I tools/ps_sweep.py sample  --spirv <translated spirv dir> --out <dir> [--n 480] [--seed 20261122]
    python -I tools/ps_sweep.py run     --spirv <spirv dir> --out <dir> --tag a --exe <vk_ps_sweep> --glslang <glslangValidator>
    VK_LOADER_DRIVERS_DISABLE=*nv* python -I tools/ps_sweep.py run ... --tag b      # another GPU (any loader filter)
    python -I tools/ps_sweep.py compare --out <dir> --tags a b

`--variant z` (default) leaves every texture lookup on the zero fallback; `--variant t` replaces the fallback images
with a gradient (VKS_TEX=1), so sampled values are not all zero. `--lo/--hi` split the sample over parallel workers
and `--only 3,7` re-runs single shaders (into `<tag>_<variant>_re`, which `compare` prefers), for example the ones that
timed out while another process was starting.

Shaders are numbered by their place in `sample.json`; the summary lines and everything under `<dir>` except
`sample.json` (which names the sampled shaders, so keep it out of reports) are counts and numbers only.
"""
import argparse
import json
import math
import os
import random
import struct
import subprocess
import sys
import time

W = H = 32
TOLERANCE = 1e-3


def load_ps(spirv):
    with open(os.path.join(spirv, "index.json"), encoding="utf-8") as f:
        index = json.load(f)
    return {k: v for k, v in index.items() if v.get("stage") == "ps"}


def stratum(meta):
    """(needs barycentrics, descriptor class, control-flow style): what decides whether and how a shader runs."""
    bary = "fragmentShaderBarycentric" in meta.get("features", [])
    b = meta.get("bindings", [])
    klass = "none" if not b else ("low" if max(b) <= 3 else "high")
    return (bary, klass, meta.get("control_flow"))


def pick_sample(ps, n, seed):
    """At least min(10, size) shaders per stratum and the rest in proportion; `n` in all, or more when the floors alone add up to more."""
    groups = {}
    for name in sorted(ps):
        groups.setdefault(stratum(ps[name]), []).append(name)
    total = sum(len(g) for g in groups.values())
    alloc = {s: min(len(g), max(10, round(n * len(g) / total))) for s, g in groups.items()}
    while sum(alloc.values()) > n:
        big = max(sorted(alloc, key=str), key=lambda s: alloc[s])
        if alloc[big] <= min(10, len(groups[big])):
            break
        alloc[big] -= 1
    rng = random.Random(seed)
    picked = []
    for s in sorted(groups, key=str):
        picked += rng.sample(groups[s], alloc[s])
    return picked, {s: (len(groups[s]), alloc[s]) for s in groups}


def vs_glsl(locations, flats):
    """A vertex shader for one pixel-shader interface: a viewport-covering triangle from the vertex index and a vec4
    at each input location (varying across the triangle, constant per vertex for the flat ones)."""
    lines = ["#version 450"]
    for loc in locations:
        lines.append(f"layout(location={loc}) {'flat ' if loc in flats else ''}out vec4 o{loc};")
    lines.append("void main() {")
    lines.append("  vec2 p = vec2(float((gl_VertexIndex << 1) & 2), float(gl_VertexIndex & 2));")
    lines.append("  gl_Position = vec4(p * 2.0 - 1.0, 0.5, 1.0);")
    for loc in locations:
        lines.append(
            f"  o{loc} = vec4(fract(0.13 * {loc + 1}.0 + p.x * 0.7), fract(0.29 * {loc + 1}.0 + p.y * 0.6),"
            f" 0.1 + 0.1 * {loc % 7}.0, 1.0);"
        )
    lines.append("}")
    return "\n".join(lines) + "\n"


def make_vs(glslang, meta, cache_dir):
    locs = sorted(meta.get("inputs", []))
    flats = set(meta.get("ps_flat_interpolants", [])) & set(locs)
    key = "vs_" + ("_".join(map(str, locs)) if locs else "none") + ("_f" + "_".join(map(str, sorted(flats))) if flats else "")
    spv = os.path.join(cache_dir, key + ".spv")
    if not os.path.exists(spv):
        src = spv[:-4] + ".vert"
        with open(src, "w", encoding="utf-8", newline="\n") as f:
            f.write(vs_glsl(locs, flats))
        r = subprocess.run([glslang, "-V", "-S", "vert", src, "-o", spv], capture_output=True, text=True)
        if r.returncode:
            raise SystemExit("glslangValidator failed: " + r.stdout[-300:])
    return spv


def cmd_sample(a):
    ps = load_ps(a.spirv)
    picked, per = pick_sample(ps, a.n, a.seed)
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "sample.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump([{"name": k, "stratum": list(stratum(ps[k]))} for k in picked], f)
    print(f"population {len(ps)} strata {len(per)} sampled {len(picked)}")
    for s in sorted(per, key=str):
        print(f"  stratum {s}: population {per[s][0]} sampled {per[s][1]}")


def run_dir(a, tag):
    return os.path.join(a.out, f"{tag}_{a.variant}{a.suffix}")


def cmd_run(a):
    ps = load_ps(a.spirv)
    with open(os.path.join(a.out, "sample.json"), encoding="utf-8") as f:
        sample = json.load(f)
    out_dir = run_dir(a, a.tag)
    os.makedirs(out_dir, exist_ok=True)
    cache = os.path.join(a.out, "vs")
    os.makedirs(cache, exist_ok=True)
    log = os.path.join(out_dir, f"results_{a.lo}.jsonl")
    done = set()
    if os.path.exists(log):
        with open(log, encoding="utf-8") as f:
            done = {json.loads(line)["i"] for line in f if line.strip()}
    hi = a.hi or len(sample)
    for i, s in enumerate(sample):
        if i < a.lo or i >= hi or i in done or (a.only and i not in a.only):
            continue
        meta = ps[s["name"]]
        raw = os.path.join(out_dir, f"{i}.raw")
        if os.path.exists(raw):
            os.unlink(raw)
        targets = max(meta.get("outputs") or [0]) + 1
        env = dict(os.environ, VKS_TIMEOUT_S=str(a.timeout), VKS_HEAP_KB="1024")
        if a.variant == "t":
            env["VKS_TEX"] = "1"
        if meta.get("needs_maintenance8"):
            env["VKS_NEED_MAINTENANCE8"] = "1"
        cmd = [a.exe, make_vs(a.glslang, meta, cache), os.path.join(a.spirv, s["name"] + ".spv"), raw,
               str(W), str(H), str(targets)]
        start = time.time()
        try:
            r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=a.timeout + 15,
                               stdin=subprocess.DEVNULL)
            code, out, err = r.returncode, r.stdout, r.stderr
        except subprocess.TimeoutExpired:
            code, out, err = 3, "", ""
        reason = ""   # a coarse reason: a missing feature or the failing Vulkan call, never a shader name
        if code == 77:
            reason = out.strip().replace("unsupported: ", "")[:80]
        elif code not in (0, 3):
            for line in err.splitlines():
                if line.startswith(("FAILED", "DEVICE_LOST", "PIPELINE")):
                    reason = line.split(" (line")[0][:60]
                    break
        rec = {"i": i, "code": code, "reason": reason, "targets": targets, "sec": round(time.time() - start, 2)}
        with open(log, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(rec) + "\n")
    print("done", a.tag, a.variant, a.lo)


def floats(path):
    with open(path, "rb") as f:
        data = f.read()
    n = len(data) // 4
    return struct.unpack(f"<{n}f", data[: n * 4])


def written_stats(vals):
    """(has NaN, every written value is zero, nothing written). The harness clears every target to -1."""
    written = [x for x in vals if x != -1.0]
    return (any(math.isnan(x) for x in vals), bool(written) and all(x == 0.0 for x in written), not written)


def classify_pair(rec_a, rec_b, raw_a, raw_b, tol=TOLERANCE):
    """The class of one shader from its two result records and output files."""
    code_a, code_b = rec_a["code"], rec_b["code"]
    if 3 in (code_a, code_b):
        return "timeout", ""
    if code_a == 0 and code_b == 0:
        fa, fb = floats(raw_a), floats(raw_b)
        if len(fa) != len(fb):
            return "fails on one GPU", "short output"
        nan_a, zero_a, none_a = written_stats(fa)
        nan_b, zero_b, none_b = written_stats(fb)
        if (nan_a, zero_a, none_a) != (nan_b, zero_b, none_b):
            return "NaN or zero on one GPU only", "nan" if nan_a != nan_b else ("zero" if zero_a != zero_b else "unwritten")
        worst, bad = 0.0, 0
        for x, y in zip(fa, fb):
            if x == y or (math.isnan(x) and math.isnan(y)):
                continue
            if not (math.isfinite(x) and math.isfinite(y)):
                # A NaN against a number, or an infinity against anything else, always differs; the tolerance
                # arithmetic would pass inf against 1.0 and NaN against NaN in another component.
                bad += 1
                worst = math.inf
                continue
            d = abs(x - y)
            if d > tol + tol * max(abs(x), abs(y)):
                bad += 1
                worst = max(worst, d)
        if bad:
            tier = "gross (>0.1)" if worst > 0.1 else ("moderate (>1e-2)" if worst > 1e-2 else "small (>1e-3)")
            return "differs beyond tolerance", tier
        return "OK on both", ""
    if code_a != 0 and code_b != 0:
        return "fails on both", f"{code_a}/{code_b}"
    bad_rec, which = (rec_a, "a") if code_a != 0 else (rec_b, "b")
    kind = "missing device feature" if bad_rec["code"] == 77 else f"exit {bad_rec['code']}"
    return "fails on one GPU", f"{which}: {kind}"


def load_results(a, tag):
    d = run_dir(a, tag)
    recs = {}
    for folder in (d, d + "_re"):   # re-runs override the first pass
        if not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            if name.startswith("results_") and name.endswith(".jsonl"):
                with open(os.path.join(folder, name), encoding="utf-8") as f:
                    for line in f:
                        if line.strip():
                            r = json.loads(line)
                            r["dir"] = folder
                            recs[r["i"]] = r
    return recs


def cmd_compare(a):
    with open(os.path.join(a.out, "sample.json"), encoding="utf-8") as f:
        sample = json.load(f)
    ta, tb = a.tags
    ra, rb = load_results(a, ta), load_results(a, tb)
    counts, sub, rows = {}, {}, []
    for i, s in enumerate(sample):
        if i not in ra or i not in rb:
            raise SystemExit(f"shader {i} has no result on both tags")
        c, sc = classify_pair(ra[i], rb[i], os.path.join(ra[i]["dir"], f"{i}.raw"), os.path.join(rb[i]["dir"], f"{i}.raw"))
        counts[c] = counts.get(c, 0) + 1
        sub[(c, sc)] = sub.get((c, sc), 0) + 1
        rows.append({"i": i, "class": c, "sub": sc, "stratum": s["stratum"], ta: ra[i]["code"], tb: rb[i]["code"],
                     ta + "_reason": ra[i]["reason"], tb + "_reason": rb[i]["reason"]})
    with open(os.path.join(a.out, f"classes_{a.variant}.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(rows, f)
    print("variant", a.variant, "total", len(sample))
    for c in sorted(counts):
        print(f"  {c}: {counts[c]}")
    for (c, sc), n in sorted(sub.items()):
        if sc:
            print(f"    {c} / {sc}: {n}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["sample", "run", "compare"])
    ap.add_argument("--spirv", help="the translated SPIR-V folder (index.json and the .spv files)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--n", type=int, default=480)
    ap.add_argument("--seed", type=int, default=20261122)
    ap.add_argument("--tag", help="a label for the GPU this run uses")
    ap.add_argument("--tags", nargs=2, default=["a", "b"])
    ap.add_argument("--variant", choices=["z", "t"], default="z")
    ap.add_argument("--exe", help="vk_ps_sweep")
    ap.add_argument("--glslang", default="glslangValidator")
    ap.add_argument("--timeout", type=int, default=20, help="seconds the GPU may take for one draw")
    ap.add_argument("--lo", type=int, default=0)
    ap.add_argument("--hi", type=int, default=0)
    ap.add_argument("--only", type=lambda s: [int(x) for x in s.split(",")], default=None)
    ap.add_argument("--suffix", default="", help="results folder suffix (a re-run uses _re)")
    a = ap.parse_args(argv)
    if a.cmd == "run" and not (a.tag and a.exe and a.spirv):
        ap.error("run needs --tag, --exe and --spirv")
    if a.cmd == "run" and a.only and not a.suffix:
        a.suffix = "_re"
    if a.cmd == "sample" and not a.spirv:
        ap.error("sample needs --spirv")
    {"sample": cmd_sample, "run": cmd_run, "compare": cmd_compare}[a.cmd](a)
    return 0


if __name__ == "__main__":
    try:  # a redirected stdout is strict ANSI code page under -I; write '?' for what it cannot encode
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError, OSError):
        pass
    sys.exit(main())
