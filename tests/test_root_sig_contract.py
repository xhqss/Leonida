"""Contract tests for tools/root_sig_contract.py (no game data, no GPU).

Run from the repository root:  python -m pytest tests/test_root_sig_contract.py
Everything is hand-built: synthetic root signatures and synthetic index
entries, so these run on every CI leg including Windows without llvm-mc.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

import root_sig_contract as rsc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "tools", "root_sig_contract.py")

ROOT_SIG = {
    "version": "1.1",
    "flags": 0,
    "params": [
        {"type": "constants", "count": 4, "visibility": "all"},
        {"type": "cbv", "visibility": "vertex"},
        {"type": "table", "ranges": [{"kind": "srv", "count": 8}],
         "visibility": "pixel"},
    ],
}


def test_pack_layout_order_and_provenance():
    dwords, error = rsc.pack_layout(ROOT_SIG)
    assert error == ""
    assert len(dwords) == 8
    assert [d["param"] for d in dwords] == [0, 0, 0, 0, 1, 1, 2, 2]
    assert [d["sub"] for d in dwords] == [0, 1, 2, 3, 0, 1, 0, 1]
    assert dwords[0]["ptype"] == "constants"
    assert dwords[4]["ptype"] == "descriptor"
    assert dwords[6]["ptype"] == "table"


def test_pack_layout_static_samplers_free():
    sig = dict(ROOT_SIG, static_samplers=[{"filter": 21}])
    dwords, error = rsc.pack_layout(sig)
    assert error == "" and len(dwords) == 8


def test_pack_layout_spill_refused():
    sig = {"version": "1.1", "flags": 0,
           "params": [{"type": "cbv"} for _ in range(17)]}
    dwords, error = rsc.pack_layout(sig)
    assert dwords == [] and "spill" in error


def test_pack_layout_cost_limit():
    sig = {"version": "1.1", "flags": 0,
           "params": [{"type": "constants", "count": 65}]}
    dwords, error = rsc.pack_layout(sig)
    assert dwords == [] and "64-dword" in error


def test_pack_layout_unknown_type():
    sig = {"version": "1.1", "flags": 0, "params": [{"type": "ufo"}]}
    dwords, error = rsc.pack_layout(sig)
    assert dwords == [] and "unknown type" in error


def test_cli_refuses_boolean_root_constant_count(tmp_path):
    index_path = tmp_path / "index.json"
    index_path.write_text(json.dumps({"synthetic": {
        "user_sgpr_reads": [{"sgpr": 0, "kind": "const32"}],
    }}), encoding="utf-8")
    sig_path = tmp_path / "sig.json"
    sig_path.write_text(json.dumps({
        "params": [{"type": "constants", "count": True}],
    }), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-I", TOOL, str(index_path), "--rootsig", str(sig_path)],
        capture_output=True, text=True)
    assert result.returncode == 1
    assert "root signature refused: param 0: constants need an integer count >= 1" in result.stdout


def test_reconcile_compatible():
    dwords, _ = rsc.pack_layout(ROOT_SIG)
    reads = [{"sgpr": 0, "kind": "const32"},
             {"sgpr": 4, "kind": "ptr64_lo"},
             {"sgpr": 5, "kind": "ptr64_hi"},
             {"sgpr": 6, "kind": "tsharp"}]
    assert rsc.reconcile(reads, dwords) == []


@pytest.mark.parametrize(("sgpr", "kind", "needle"), [
    (0, "ptr64_lo", "incompatible"),  # s0 is a constant dword, not a VA
    (31, "const32", "8 dwords"),  # layout ends at s7
    (4, "ptr64_hi", "paired lo"),  # s4 is sub 0 of the CBV pair
    (5, "ptr64_lo", "high half"),  # s5 is the high half of the CBV pair
    (7, "ptr64_lo", "high half"),  # s7 is the high half of the table pair
    (0, "nope", "unknown read kind"),
    (-1, "tsharp", "8 dwords"),  # negative index must not alias last table dword
    (-9, "tsharp", "8 dwords"),  # beyond layout must not crash
])
def test_reconcile_contradictions(sgpr, kind, needle):
    dwords, _ = rsc.pack_layout(ROOT_SIG)
    issues = rsc.reconcile([{"sgpr": sgpr, "kind": kind}], dwords)
    assert len(issues) == 1 and needle in issues[0]


def test_cluster_groups_shapes():
    index = {
        "a": {"user_sgpr_reads": [{"sgpr": 0, "kind": "const32"}]},
        "b": {"user_sgpr_reads": [{"sgpr": 0, "kind": "const32"}]},
        "c": {"user_sgpr_reads": [{"sgpr": 4, "kind": "ptr64_lo"}]},
    }
    ranked = rsc.cluster(index)
    assert ranked[0][1] == 2 and ranked[1][1] == 1


def test_cli_hard_fail_on_contradiction(tmp_path):
    index = {"bad": {"user_sgpr_reads": [{"sgpr": 31, "kind": "const32"}]}}
    index_path = tmp_path / "index.json"
    index_path.write_text(json.dumps(index), encoding="utf-8")
    sig_path = tmp_path / "sig.json"
    sig_path.write_text(json.dumps(ROOT_SIG), encoding="utf-8")
    r = subprocess.run(
        [sys.executable, "-I", TOOL, str(index_path),
         "--rootsig", str(sig_path)],
        capture_output=True, text=True)
    assert r.returncode == 1
    assert "CONTRADICTION bad" in r.stdout


@pytest.mark.parametrize(("sgpr", "status", "diagnostic"), [
    (4, 0, "1 compatible"),
    (5, 1, "s5 (ptr32_lo) reads the high half of the address"),
    (6, 0, "1 compatible"),
    (7, 1, "s7 (ptr32_lo) reads the high half of the address"),
])
def test_cli_32bit_pointer_must_use_low_address_slot(
    tmp_path: Path, sgpr: int, status: int, diagnostic: str,
) -> None:
    index = {"shader": {"user_sgpr_reads": [{"sgpr": sgpr, "kind": "ptr32_lo"}]}}
    index_path = tmp_path / "index.json"
    index_path.write_text(json.dumps(index), encoding="utf-8")
    sig_path = tmp_path / "sig.json"
    sig_path.write_text(json.dumps(ROOT_SIG), encoding="utf-8")
    result = subprocess.run([sys.executable, "-I", TOOL, str(index_path), "--rootsig", str(sig_path)],
                            capture_output=True, text=True)
    assert result.returncode == status, result.stdout + result.stderr
    assert diagnostic in result.stdout


@pytest.mark.parametrize("bad_index", ["not json", "[]", '{"a": 1}'])
def test_bad_index_shape_is_one_error_not_a_traceback(tmp_path, bad_index):
    index_path = tmp_path / "index.json"
    index_path.write_text(bad_index, encoding="utf-8")
    sig_path = tmp_path / "sig.json"
    sig_path.write_text(json.dumps(ROOT_SIG), encoding="utf-8")
    r = subprocess.run([sys.executable, "-I", TOOL, str(index_path), "--rootsig", str(sig_path)],
                       capture_output=True, text=True)
    assert r.returncode == 2
    assert "ERROR:" in r.stderr
    assert "Traceback" not in r.stderr


@pytest.mark.parametrize("bad_sig", ["not json", "[]"])
def test_bad_rootsig_shape_is_one_error_not_a_traceback(tmp_path, bad_sig):
    index_path = tmp_path / "index.json"
    index_path.write_text("{}", encoding="utf-8")
    sig_path = tmp_path / "sig.json"
    sig_path.write_text(bad_sig, encoding="utf-8")
    r = subprocess.run([sys.executable, "-I", TOOL, str(index_path), "--rootsig", str(sig_path)],
                       capture_output=True, text=True)
    assert r.returncode == 2
    assert "ERROR:" in r.stderr
    assert "Traceback" not in r.stderr


def test_missing_files_are_one_error(tmp_path):
    r = subprocess.run([sys.executable, "-I", TOOL, str(tmp_path / "missing_index.json"),
                        "--rootsig", str(tmp_path / "missing_sig.json")],
                       capture_output=True, text=True)
    assert r.returncode == 2
    assert "ERROR: cannot read" in r.stderr
    assert "Traceback" not in r.stderr


@pytest.mark.parametrize("bad_params", ["abc", {"type": "cbv"}, [1], ["cbv"]])
def test_bad_param_shapes_are_one_error_not_a_traceback(tmp_path, bad_params):
    index_path = tmp_path / "index.json"
    index_path.write_text("{}", encoding="utf-8")
    sig_path = tmp_path / "sig.json"
    sig_path.write_text(json.dumps({"params": bad_params}), encoding="utf-8")
    r = subprocess.run([sys.executable, "-I", TOOL, str(index_path), "--rootsig", str(sig_path)],
                       capture_output=True, text=True)
    assert r.returncode == 2
    assert "ERROR:" in r.stderr
    assert "Traceback" not in r.stderr


@pytest.mark.parametrize("bad_reads", [
    3, {"sgpr": 0}, ["x"], [{"kind": "const32"}], [{"sgpr": 0}],
    [{"sgpr": 0, "kind": ["const32"]}], [{"sgpr": 0, "kind": {"a": 1}}],
])
def test_bad_read_shapes_are_one_error_not_a_traceback(tmp_path, bad_reads):
    index_path = tmp_path / "index.json"
    index_path.write_text(json.dumps({"s": {"user_sgpr_reads": bad_reads}}), encoding="utf-8")
    sig_path = tmp_path / "sig.json"
    sig_path.write_text(json.dumps(ROOT_SIG), encoding="utf-8")
    r = subprocess.run([sys.executable, "-I", TOOL, str(index_path), "--rootsig", str(sig_path)],
                       capture_output=True, text=True)
    assert r.returncode == 2
    assert "ERROR:" in r.stderr
    assert "Traceback" not in r.stderr


def test_cli_hard_fail_on_negative_sgpr(tmp_path):
    index = {"bad": {"user_sgpr_reads": [{"sgpr": -1, "kind": "tsharp"}]}}
    index_path = tmp_path / "index.json"
    index_path.write_text(json.dumps(index), encoding="utf-8")
    sig_path = tmp_path / "sig.json"
    sig_path.write_text(json.dumps(ROOT_SIG), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-I", TOOL, str(index_path),
         "--rootsig", str(sig_path)],
        capture_output=True, text=True)
    assert result.returncode == 1
    assert "CONTRADICTION bad" in result.stdout
    assert "s-1 read but layout has 8 dwords" in result.stdout


@pytest.mark.parametrize("sgpr", [True, False, 1.0, "1", None, [], {}])
def test_reconcile_rejects_noninteger_sgpr(sgpr):
    dwords, _ = rsc.pack_layout(ROOT_SIG)
    # Including a real index catches bool/int and float/int key collisions.
    reads = [{"sgpr": 1, "kind": "const32"},
             {"sgpr": sgpr, "kind": "const32"}]
    assert rsc.reconcile(reads, dwords) == [
        f"invalid SGPR index {sgpr!r}: expected an integer"]


def test_cluster_preserves_invalid_index_distinctions():
    index = {
        "valid": {"user_sgpr_reads": [{"sgpr": 1, "kind": "const32"}]},
        "invalid": {"user_sgpr_reads": [
            {"sgpr": 1, "kind": "const32"},
            {"sgpr": True, "kind": "const32"},
            {"sgpr": "1", "kind": "const32"},
            {"sgpr": [], "kind": "const32"}]},
    }
    ranked = rsc.cluster(index)
    assert len(ranked) == 2
    assert sum(count for _, count, _ in ranked) == 2

    # Valid SGPRs must keep numeric order even when an invalid index is present.
    index["mixed"] = {"user_sgpr_reads": [
        {"sgpr": 10, "kind": "const32"},
        {"sgpr": "bad", "kind": "const32"},
        {"sgpr": 2, "kind": "const32"}]}
    mixed_shape = next(shape for shape, _, names in rsc.cluster(index)
                       if names == ["mixed"])
    assert mixed_shape == ((2, "const32"), (10, "const32"),
                           ("invalid:'bad'", "const32"))


def test_cli_rejects_noninteger_sgprs_with_cluster(tmp_path):
    index = {"bad": {"user_sgpr_reads": [
        {"sgpr": 1, "kind": "const32"},
        {"sgpr": True, "kind": "const32"},
        {"sgpr": "1", "kind": "const32"},
        {"sgpr": 1.0, "kind": "const32"},
        {"sgpr": [], "kind": "const32"}]}}
    index_path = tmp_path / "index.json"
    index_path.write_text(json.dumps(index), encoding="utf-8")
    sig_path = tmp_path / "sig.json"
    sig_path.write_text(json.dumps(ROOT_SIG), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-I", TOOL, str(index_path),
         "--rootsig", str(sig_path), "--cluster"],
        capture_output=True, text=True)
    assert result.returncode == 1
    assert "CONTRADICTION bad" in result.stdout
    assert "invalid SGPR index" in result.stdout
    assert "read-set shapes" in result.stdout
    assert "Traceback" not in result.stderr
