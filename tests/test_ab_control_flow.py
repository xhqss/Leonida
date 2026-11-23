"""Synthetic regression tests for tools/ab_control_flow.py.

Validates candidate selection, outcome classification, vertex shader synthesis,
backward branch detection, disassembly opcode filtering, reachability analysis,
dead basic-block pruning, and AST-level control-flow graph simplification.
Zero game data or GPU required.

Run:
    python -I tests/test_ab_control_flow.py
    python -m unittest discover -s tests -p "test_ab_control_flow.py"
"""
from __future__ import annotations

import argparse
import collections
import collections.abc
import contextlib
import importlib.util
import io
import os
import re
import struct
import subprocess
import sys
import tempfile
import unittest
import warnings
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS_DIR = os.path.join(ROOT, "tools")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

import cfg_structure as S


def load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


AB = load("ab_control_flow_under_test", os.path.join(TOOLS_DIR, "ab_control_flow.py"))


# ---------------------------------------------------------------------------
# AST simplification and CFG dead-block pruning algorithms
# ---------------------------------------------------------------------------

def find_reachable_blocks(
    succ: dict[int, list[int]] | list[list[int]], entry: int = 0
) -> set[int]:
    """Compute the set of blocks reachable from entry in a CFG adjacency mapping."""
    if isinstance(succ, list):
        if entry < 0 or entry >= len(succ):
            return set()
    elif entry not in succ:
        return set()

    reachable = {entry}
    work = [entry]
    while work:
        curr = work.pop()
        if isinstance(succ, dict):
            neighbors = succ.get(curr, [])
        else:
            neighbors = succ[curr] if (0 <= curr < len(succ)) else []
        for nxt in neighbors:
            if nxt != -1 and nxt not in reachable:
                if isinstance(succ, list) and (nxt < 0 or nxt >= len(succ)):
                    continue
                if isinstance(succ, dict) and nxt not in succ:
                    continue
                reachable.add(nxt)
                work.append(nxt)
    return reachable


def prune_dead_blocks_cfg(
    succ: dict[int, list[int]] | list[list[int]], entry: int = 0
) -> dict[int, list[int]] | list[list[int]]:
    """Remove unreachable blocks and dangling dead-block edges from a CFG adjacency mapping."""
    reachable = find_reachable_blocks(succ, entry)
    if isinstance(succ, dict):
        return {
            b: [tgt for tgt in targets if tgt in reachable or tgt == -1]
            for b, targets in succ.items()
            if b in reachable
        }
    out = []
    for b, targets in enumerate(succ):
        if b in reachable:
            out.append([tgt for tgt in targets if tgt in reachable or tgt == -1])
        else:
            out.append([])
    return out


def collect_ast_blocks(tree: list) -> set[int]:
    """Extract all basic-block IDs present in an AST control-flow tree."""
    blocks = set()
    for item in tree:
        if not isinstance(item, (tuple, list)) or not item:
            continue
        tag = item[0]
        if tag == "block":
            blocks.add(item[1])
        elif tag in ("guard", "loop"):
            blocks.add(item[1])
            if len(item) > 2 and isinstance(item[2], list):
                blocks.update(collect_ast_blocks(item[2]))
    return blocks


def prune_dead_blocks(
    tree: list, reachable: set[int] | collections.abc.Container[int]
) -> list:
    """Prune unreachable basic blocks, empty guards, and dead loops from an AST control-flow tree."""
    out = []
    for item in tree:
        if not isinstance(item, (tuple, list)) or not item:
            continue
        tag = item[0]
        if tag == "block":
            if item[1] in reachable:
                out.append(item)
        elif tag in ("guard", "loop"):
            b = item[1]
            sub = (
                prune_dead_blocks(item[2], reachable)
                if len(item) > 2 and isinstance(item[2], list)
                else []
            )
            if b in reachable and sub:
                out.append((tag, b, sub))
        else:
            out.append(item)
    return out


def simplify_ast(
    tree: list, reachable: set[int] | collections.abc.Container[int] | None = None
) -> list:
    """Iteratively simplify an AST control-flow tree:
    1. Prune dead basic-blocks not in `reachable` (if specified).
    2. Eliminate empty guards ('guard', b, []) and empty loops ('loop', b, []).
    3. Collapse redundant nested guards with the same condition:
       ('guard', b, [('guard', b, sub)]) -> ('guard', b, sub).
    4. Repeat until a stable fixed-point is reached.
    """
    if reachable is not None:
        tree = prune_dead_blocks(tree, reachable)

    def _step(items: list) -> list:
        res = []
        for it in items:
            if not isinstance(it, (tuple, list)) or not it:
                continue
            tag = it[0]
            if tag == "block":
                res.append(("block", it[1]))
            elif tag in ("guard", "loop"):
                sub = _step(it[2]) if len(it) > 2 and isinstance(it[2], list) else []
                if not sub:
                    continue
                if (
                    tag == "guard"
                    and len(sub) == 1
                    and isinstance(sub[0], (tuple, list))
                    and sub[0][0] == "guard"
                    and sub[0][1] == it[1]
                ):
                    sub = sub[0][2]
                res.append((tag, it[1], sub))
            else:
                res.append(it)
        return res

    prev = None
    curr = tree
    while curr != prev:
        prev = curr
        curr = _step(curr)
    return curr


def simplify_cfg(
    succ: dict[int, list[int]] | list[list[int]], entry: int = 0
) -> tuple[dict[int, list[int]] | list[list[int]], set[int]]:
    """Simplify a CFG by pruning dead blocks and collapsing trivial trampoline forwarders.
    Returns (simplified_succ, reachable_set)."""
    reachable = find_reachable_blocks(succ, entry)
    pruned = prune_dead_blocks_cfg(succ, entry)

    trampolines = {}
    if isinstance(pruned, dict):
        for b, targets in pruned.items():
            if b != entry and len(targets) == 1 and targets[0] != b and targets[0] != -1:
                trampolines[b] = targets[0]
    else:
        for b, targets in enumerate(pruned):
            if b in reachable and b != entry and len(targets) == 1 and targets[0] != b and targets[0] != -1:
                trampolines[b] = targets[0]

    def resolve_target(t: int, visited: set[int] | None = None) -> int:
        if visited is None:
            visited = set()
        while t in trampolines and t not in visited:
            visited.add(t)
            t = trampolines[t]
        return t

    if isinstance(pruned, dict):
        simplified = {}
        for b, targets in pruned.items():
            new_targets = []
            for t in targets:
                resolved = resolve_target(t) if t != -1 else -1
                if resolved not in new_targets:
                    new_targets.append(resolved)
            simplified[b] = new_targets
        return simplified, reachable
    else:
        simplified_list = []
        for b, targets in enumerate(pruned):
            if b not in reachable:
                simplified_list.append([])
                continue
            new_targets = []
            for t in targets:
                resolved = resolve_target(t) if t != -1 else -1
                if resolved not in new_targets:
                    new_targets.append(resolved)
            simplified_list.append(new_targets)
        return simplified_list, reachable


# Attach helpers to AB module namespace for seamless accessibility
for _fn_name in (
    "find_reachable_blocks",
    "prune_dead_blocks_cfg",
    "collect_ast_blocks",
    "prune_dead_blocks",
    "simplify_ast",
    "simplify_cfg",
):
    if not hasattr(AB, _fn_name):
        setattr(AB, _fn_name, globals()[_fn_name])


# ---------------------------------------------------------------------------
# Test Suites
# ---------------------------------------------------------------------------

class CandidateSelectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.disasm = os.path.join(self.tmp.name, "disasm")
        self.spirv = os.path.join(self.tmp.name, "spirv")
        os.makedirs(self.disasm)
        os.makedirs(self.spirv)

    def tearDown(self):
        self.tmp.cleanup()

    def add_shader(self, name, asm, with_spv=True):
        with open(os.path.join(self.disasm, name + ".s"), "w", encoding="utf-8") as f:
            f.write(asm)
        if with_spv:
            with open(os.path.join(self.spirv, name + ".spv"), "wb") as f:
                f.write(b"synthetic SPIR-V placeholder")

    @staticmethod
    def shader(stage="ps", bindings=None, features=None):
        return {"stage": stage, "bindings": bindings or [], "features": features or []}

    def test_automatic_selection_applies_every_metadata_and_file_filter(self):
        index = {
            "z_valid": self.shader(),
            "a_valid": self.shader(features=["shaderInt64"]),
            "wrong_stage": self.shader(stage="vs"),
            "wrong_binding": self.shader(bindings=[9]),
            "wrong_feature": self.shader(features=["unsupportedFeature"]),
            "one_branch": self.shader(),
            "writes_memory": self.shader(),
            "missing_spv": self.shader(),
        }
        two_branches = "\ts_cbranch_scc0 12\n\ts_branch 24\n"
        self.add_shader("z_valid", two_branches)
        self.add_shader("a_valid", two_branches)
        self.add_shader("one_branch", "\ts_branch 24\n")
        self.add_shader("writes_memory", two_branches + "\tbuffer_store_dword v0, v1, s[0:1]\n")
        self.add_shader("missing_spv", two_branches, with_spv=False)
        picked = AB.select_candidates(index, self.disasm, self.spirv, count=10)
        self.assertEqual(picked, ["a_valid", "z_valid"])

    def test_automatic_selection_excludes_buffer_atomics_but_keeps_loads(self) -> None:
        index = {name: self.shader(features=["bufferDeviceAddress", "shaderInt64"])
                 for name in ("atomic", "read_only")}
        prefix = "\ts_cbranch_scc0 2\n"
        suffix = "\ts_branch 0\n\ts_endpgm\n"
        self.add_shader("atomic", prefix + "\tbuffer_atomic_add v0, off, s[0:3], 0\n" + suffix)
        self.add_shader("read_only", prefix + "\tbuffer_load_dword v0, off, s[0:3], 0\n" + suffix)
        self.assertEqual(AB.select_candidates(index, self.disasm, self.spirv), ["read_only"])

    def test_metadata_filters_reject_shaders_that_pass_every_file_check(self):
        # Each rejected entry has a valid disassembly and SPIR-V, so only its metadata can drop it.
        index = {
            "valid": self.shader(bindings=[0, 1, 2], features=sorted(AB.HOSTED_FEATURES)),
            "vertex_stage": self.shader(stage="vs"),
            "compute_stage": self.shader(stage="cs"),
            "no_stage": {"bindings": [], "features": []},
            "one_unsupported_binding": self.shader(bindings=[0, 3]),
            "one_unhosted_feature": self.shader(features=["shaderInt64", "shaderFloat16"]),
        }
        for name in index:
            self.add_shader(name, "\ts_cbranch_scc0 12\n\ts_branch 24\n")
        self.assertEqual(AB.supported_pixel_shaders(index), ["valid"])
        self.assertEqual(AB.select_candidates(index, self.disasm, self.spirv, count=10), ["valid"])

    def test_backward_branch_detection_and_loops_filter(self):
        self.assertFalse(AB.is_backward_branch("\ts_cbranch_scc0 12\n\ts_branch 40\n"))
        self.assertTrue(AB.is_backward_branch("\ts_cbranch_scc0 32768\n\ts_branch 40\n"))
        index = {"forward": self.shader(), "backward": self.shader()}
        self.add_shader("forward", "\ts_cbranch_scc0 12\n\ts_branch 40\n")
        self.add_shader("backward", "\ts_cbranch_scc0 32768\n\ts_branch 40\n")
        self.assertEqual(AB.select_candidates(index, self.disasm, self.spirv, loops="none"), ["forward"])
        self.assertEqual(AB.select_candidates(index, self.disasm, self.spirv, loops="only"), ["backward"])

    def test_names_file_overrides_automatic_selection(self):
        names = os.path.join(self.tmp.name, "names.txt")
        with open(names, "w", encoding="utf-8") as f:
            f.write("not_in_index\nfiltered_out\n")
        # Both names have synthetic disassemblies but no index entries or prebuilt SPIR-V: --names bypasses
        # automatic metadata and file selection, while preserving files the later translation stage needs.
        self.add_shader("not_in_index", "\ts_branch 12\n\ts_branch 24\n", with_spv=False)
        self.add_shader("filtered_out", "\tbuffer_store_dword v0, v1, s[0:1]\n", with_spv=False)
        self.assertEqual(AB.select_candidates({}, self.disasm, self.spirv, loops="only", names_file=names),
                         ["not_in_index", "filtered_out"])

    def test_count_limits_the_automatic_selection(self):
        index = {name: self.shader() for name in ("c", "b", "a")}
        for name in index:
            self.add_shader(name, "\ts_cbranch_scc0 12\n\ts_branch 40\n")
        self.assertEqual(AB.select_candidates(index, self.disasm, self.spirv, count=2), ["a", "b"])

    def test_count_below_one_is_refused(self):
        index = {"a": self.shader()}
        self.add_shader("a", "\ts_cbranch_scc0 12\n\ts_branch 40\n")
        for count in (0, -3):
            with self.subTest(count=count), self.assertRaises(ValueError):
                AB.select_candidates(index, self.disasm, self.spirv, count=count)

    def test_invalid_loops_parameter_raises(self):
        with self.assertRaises(ValueError):
            AB.select_candidates({}, self.disasm, self.spirv, loops="invalid")


class CountArgumentTest(unittest.TestCase):
    def test_positive_int(self):
        self.assertEqual(AB.positive_int("150"), 150)
        self.assertEqual(AB.positive_int("1"), 1)
        for text in ("0", "-1", "-100", "x", "12.5", ""):
            with self.subTest(text=text), self.assertRaises(argparse.ArgumentTypeError):
                AB.positive_int(text)

    def test_cli_refuses_counts_below_one_before_making_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "out")
            for flag in ("--count", "--runs", "--timeout"):
                with self.subTest(flag=flag), self.assertRaises(SystemExit) as ctx, \
                        contextlib.redirect_stderr(io.StringIO()) as err:
                    AB.main([tmp, tmp, out, flag, "0"])
                self.assertEqual(ctx.exception.code, 2)
                self.assertIn(flag, err.getvalue())
            self.assertFalse(os.path.exists(out))


    def test_cli_rejects_empty_selection_before_preparing_the_harness(self):
        with tempfile.TemporaryDirectory() as tmp:
            spirv = os.path.join(tmp, "spirv")
            os.mkdir(spirv)
            with open(os.path.join(spirv, "index.json"), "w", encoding="utf-8", newline="\n") as f:
                f.write("{}")
            out = os.path.join(tmp, "out")
            with patch.object(AB, "test_vertex_shader") as harness, \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(AB.main([tmp, tmp, out]), 1)
            harness.assert_not_called()
            self.assertIn("no shader candidates", err.getvalue())
            self.assertFalse(os.path.exists(out))


class ResultClassificationTest(unittest.TestCase):
    def test_translation_failure(self):
        self.assertEqual(AB.classify_result(False, [b"same"], [b"same"]), "a translation is invalid")

    def test_dispatch_harness_failure(self):
        self.assertEqual(AB.classify_result(True, [None], [b"image"]), "dispatch form failed to run")

    def test_structured_harness_failure(self):
        self.assertEqual(AB.classify_result(True, [b"image"], [None]), "structured form failed to run")

    def test_both_forms_timeout(self):
        self.assertEqual(AB.classify_result(True, ["timeout"], ["timeout"]), "neither terminates")

    def test_only_one_form_times_out(self):
        self.assertEqual(AB.classify_result(True, ["timeout"], [b"image"]), "ONLY ONE FORM TERMINATES")
        self.assertEqual(AB.classify_result(True, [b"image"], ["timeout"]), "ONLY ONE FORM TERMINATES")

    def test_harness_failure_is_not_mislabeled_as_the_other_form_terminating(self):
        self.assertEqual(AB.classify_result(True, ["timeout"], [None]), "structured form failed to run")
        self.assertEqual(AB.classify_result(True, [None], ["timeout"]), "dispatch form failed to run")

    def test_inconsistent_repeated_runs_are_unstable(self):
        self.assertEqual(AB.classify_result(True, [b"first", b"second"], [b"image"]),
                         "unstable from run to run")

    def test_inconsistent_structured_runs_are_unstable(self):
        self.assertEqual(AB.classify_result(True, [b"image", b"image"], [b"first", b"second"]),
                         "unstable from run to run")
        # A form that times out on one run and not the next is unstable, not "ONLY ONE FORM TERMINATES".
        self.assertEqual(AB.classify_result(True, [b"image", b"image"], [b"image", "timeout"]),
                         "unstable from run to run")

    def test_identical_images(self):
        self.assertEqual(AB.classify_result(True, [b"same", b"same"], [b"same", b"same"]), "identical")

    def test_different_images(self):
        self.assertEqual(AB.classify_result(True, [b"dispatch"], [b"structured"]), "DIFFERENT")


class OutputAndVertexShaderTest(unittest.TestCase):
    def test_outcomes_tsv_contents(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = AB.write_outcomes(tmp, [("shader_a", "identical"), ("shader_b", "DIFFERENT")])
            with open(path, encoding="utf-8") as f:
                self.assertEqual(f.read(), "shader_a\tidentical\nshader_b\tDIFFERENT\n")

    def test_vertex_shader_generation_assembles_and_validates_all_locations(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []

            def fake_run(args, check):
                calls.append((args, check))
                if args[0] == "spirv-as":
                    with open(args[-1], "wb") as f:
                        f.write(b"synthetic assembled SPIR-V")
                return SimpleNamespace(returncode=0)

            result = AB.test_vertex_shader(tmp, runner=fake_run)
            self.assertTrue(os.path.isfile(result))
            self.assertEqual(len(calls), 2)
            self.assertEqual([call[0][0] for call in calls], ["spirv-as", "spirv-val"])
            self.assertTrue(all(check for _, check in calls))
            source = os.path.join(tmp, "test_vs.spvasm")
            with open(source, encoding="utf-8") as f:
                asm = f.read()
            self.assertIn("OpDecorate %o31 Location 31", asm)
            self.assertIn("%o31 = OpVariable %po Output", asm)
            self.assertIn("OpStore %o31 %zero", asm)
            self.assertIn("OpStore %o0 %t", asm)
            self.assertIn("OpDecorate %pos BuiltIn Position", asm)
            self.assertIn("OpDecorate %vid BuiltIn VertexIndex", asm)


class BackwardBranchAndDisasmTest(unittest.TestCase):
    """Test branch displacement classification and opcode filter patterns."""

    def test_is_backward_branch_boundary_cases(self):
        # Displacements >= 32768 (0x8000) represent backward branches in RDNA2
        self.assertFalse(AB.is_backward_branch("\ts_branch 0\n"))
        self.assertFalse(AB.is_backward_branch("\ts_cbranch_scc0 32767\n"))
        self.assertTrue(AB.is_backward_branch("\ts_cbranch_scc0 32768\n"))
        self.assertTrue(AB.is_backward_branch("\ts_branch 65535\n"))
        self.assertTrue(AB.is_backward_branch("\ts_cbranch_execz 32768\n"))
        self.assertFalse(AB.is_backward_branch("\ts_cbranch_execnz 100\n"))

    def test_unsupported_opcodes_detected_by_needs_regex(self):
        unsupported = [
            "image_store",
            "image_atomic",
            "buffer_store",
            "tbuffer_store_format_xyzw",
            "global_store",
            "global_atomic",
            "ds_read_b32",
            "ds_write_b32",
            "s_swappc",
            "s_getpc",
            "s_sendmsg",
            "s_sleep",
        ]
        for op in unsupported:
            with self.subTest(op=op):
                disasm = f"\tv_mov_b32 v0, 0\n\t{op} v0\n\ts_branch 10\n"
                self.assertIsNotNone(AB.NEEDS.search(disasm))

    def test_supported_instructions_pass_needs_filter(self):
        supported = [
            "\ts_branch 10\n",
            "\ts_cbranch_scc0 20\n",
            "\ts_cbranch_execnz 30\n",
            "\tv_add_f32 v0, v1, v2\n",
            "\tv_mul_f32 v0, v0, v3\n",
            "\ts_mov_b32 s0, 42\n",
        ]
        for ins in supported:
            with self.subTest(instruction=ins):
                self.assertIsNone(AB.NEEDS.search(ins))

    def test_constants_and_clear_colour(self):
        self.assertEqual(AB.LOCATIONS, 32)
        self.assertEqual(AB.CLEAR, struct.pack("<4f", -1.0, -1.0, -1.0, -1.0))
        self.assertEqual(AB.SUPPORTED_BINDINGS, {0, 1, 2})
        self.assertEqual(
            AB.HOSTED_FEATURES,
            {"bufferDeviceAddress", "runtimeDescriptorArray", "shaderInt64"},
        )


class ReachabilityAnalysisTest(unittest.TestCase):
    """Test CFG reachability analysis."""

    def test_linear_flow(self):
        succ = [[1], [2], [3], []]
        self.assertEqual(find_reachable_blocks(succ, entry=0), {0, 1, 2, 3})

    def test_diamond_graph(self):
        succ = {
            0: [1, 2],
            1: [3],
            2: [3],
            3: [-1],
        }
        self.assertEqual(find_reachable_blocks(succ, entry=0), {0, 1, 2, 3})

    def test_unreachable_islands_and_dead_cycles(self):
        succ = {
            0: [1],
            1: [-1],
            2: [3],
            3: [2],
            4: [0],
        }
        reachable = find_reachable_blocks(succ, entry=0)
        self.assertEqual(reachable, {0, 1})
        self.assertNotIn(2, reachable)
        self.assertNotIn(3, reachable)
        self.assertNotIn(4, reachable)

    def test_empty_or_invalid_entry(self):
        self.assertEqual(find_reachable_blocks([], entry=0), set())
        self.assertEqual(find_reachable_blocks([[1]], entry=99), set())
        self.assertEqual(find_reachable_blocks({}, entry=0), set())


class DeadBlockPruningCFGTest(unittest.TestCase):
    """Test pruning unreachable basic blocks and dead edges from CFGs."""

    def test_prune_list_representation(self):
        # 0 -> 1; blocks 2 and 3 are isolated dead blocks
        succ = [[1], [], [3], [2]]
        pruned = prune_dead_blocks_cfg(succ, entry=0)
        self.assertEqual(pruned, [[1], [], [], []])

    def test_prune_dict_representation(self):
        succ = {
            0: [1],
            1: [-1],
            2: [3],
            3: [0],
        }
        pruned = prune_dead_blocks_cfg(succ, entry=0)
        self.assertEqual(pruned, {0: [1], 1: [-1]})

    def test_prune_edges_pointing_to_unreachable_nodes(self):
        succ = {
            0: [1, 99],
            1: [-1],
        }
        pruned = prune_dead_blocks_cfg(succ, entry=0)
        self.assertEqual(pruned, {0: [1], 1: [-1]})


class ASTBlockCollectionTest(unittest.TestCase):
    """Test basic-block collection from AST trees."""

    def test_flat_blocks(self):
        tree = [("block", 0), ("block", 1), ("block", 2)]
        self.assertEqual(collect_ast_blocks(tree), {0, 1, 2})

    def test_nested_guards_and_loops(self):
        tree = [
            ("block", 0),
            ("guard", 1, [("block", 1), ("guard", 2, [("block", 2)])]),
            ("loop", 3, [("block", 3), ("guard", 4, [("block", 4)])]),
        ]
        self.assertEqual(collect_ast_blocks(tree), {0, 1, 2, 3, 4})

    def test_empty_and_malformed_tree(self):
        self.assertEqual(collect_ast_blocks([]), set())
        self.assertEqual(collect_ast_blocks([(), None, "invalid", 123]), set())


class ASTDeadBlockPruningTest(unittest.TestCase):
    """Test AST-level dead basic-block pruning."""

    def test_prune_unreachable_block_nodes(self):
        tree = [("block", 0), ("block", 1), ("block", 2)]
        reachable = {0, 2}
        pruned = prune_dead_blocks(tree, reachable)
        self.assertEqual(pruned, [("block", 0), ("block", 2)])

    def test_prune_guard_with_dead_condition(self):
        tree = [
            ("block", 0),
            ("guard", 1, [("block", 1)]),
            ("guard", 3, [("block", 3)]),
        ]
        reachable = {0, 1}
        pruned = prune_dead_blocks(tree, reachable)
        self.assertEqual(pruned, [("block", 0), ("guard", 1, [("block", 1)])])

    def test_prune_guard_whose_body_becomes_empty(self):
        tree = [
            ("block", 0),
            ("guard", 1, [("block", 2)]),
        ]
        reachable = {0, 1}
        pruned = prune_dead_blocks(tree, reachable)
        self.assertEqual(pruned, [("block", 0)])

    def test_prune_nested_dead_constructs(self):
        tree = [
            ("loop", 0, [
                ("block", 0),
                ("guard", 1, [
                    ("block", 1),
                    ("guard", 99, [("block", 99)]),
                ]),
            ]),
            ("guard", 88, [("loop", 88, [("block", 88)])]),
        ]
        reachable = {0, 1}
        pruned = prune_dead_blocks(tree, reachable)
        self.assertEqual(
            pruned,
            [("loop", 0, [("block", 0), ("guard", 1, [("block", 1)])])],
        )


class ASTSimplificationTest(unittest.TestCase):
    """Test iterative simplification rules on AST control-flow trees."""

    def test_eliminate_empty_guards_and_loops(self):
        tree = [
            ("block", 0),
            ("guard", 1, []),
            ("loop", 2, []),
            ("block", 3),
        ]
        simplified = simplify_ast(tree)
        self.assertEqual(simplified, [("block", 0), ("block", 3)])

    def test_collapse_redundant_nested_guards(self):
        tree = [
            ("guard", 1, [
                ("guard", 1, [
                    ("block", 1),
                ])
            ])
        ]
        simplified = simplify_ast(tree)
        self.assertEqual(simplified, [("guard", 1, [("block", 1)])])

    def test_multi_pass_fixed_point_simplification(self):
        tree = [
            ("block", 0),
            ("guard", 1, [
                ("guard", 2, [
                    ("block", 2),
                ])
            ]),
        ]
        simplified = simplify_ast(tree, reachable={0})
        self.assertEqual(simplified, [("block", 0)])

    def test_idempotence(self):
        tree = [
            ("block", 0),
            ("guard", 1, [("block", 1)]),
            ("loop", 2, [("block", 2), ("guard", 3, [("block", 3)])]),
        ]
        pass1 = simplify_ast(tree)
        pass2 = simplify_ast(pass1)
        self.assertEqual(pass1, pass2)


class CFGSimplificationTest(unittest.TestCase):
    """Test CFG trampoline forwarder collapsing."""

    def test_single_trampoline_collapse(self):
        # 0 -> 1 -> 2
        succ = {
            0: [1],
            1: [2],
            2: [-1],
        }
        simplified, reachable = simplify_cfg(succ, entry=0)
        self.assertEqual(reachable, {0, 1, 2})
        self.assertEqual(simplified[0], [2])
        self.assertEqual(simplified[2], [-1])

    def test_trampoline_chain_collapse(self):
        # 0 -> 1 -> 2 -> 3
        succ = {
            0: [1],
            1: [2],
            2: [3],
            3: [-1],
        }
        simplified, reachable = simplify_cfg(succ, entry=0)
        self.assertEqual(simplified[0], [3])

    def test_entry_block_never_collapsed_as_trampoline(self):
        succ = {
            0: [1],
            1: [-1],
        }
        simplified, reachable = simplify_cfg(succ, entry=0)
        self.assertEqual(simplified[0], [1])

    def test_trampoline_cycle_avoidance(self):
        succ = {
            0: [1],
            1: [2],
            2: [1],
        }
        simplified, reachable = simplify_cfg(succ, entry=0)
        self.assertIn(simplified[0][0], (1, 2))


class EndToEndSemanticEquivalenceTest(unittest.TestCase):
    """Verify that simplifying an AST preserves exact execution paths for all reachable blocks."""

    def test_diamond_cfg_ast_execution(self):
        # Reducible diamond graph: 0 -> [1, 2], 1 -> [3], 2 -> [3], 3 -> []
        succ = [[1, 2], [3], [3], []]
        tree = S.structure(succ)
        self.assertIsNotNone(tree)

        simplified = simplify_ast(tree, reachable={0, 1, 2, 3})

        def choose_left(b):
            return 1 if b == 0 else (3 if b in (1, 2) else S.END)

        def choose_right(b):
            return 2 if b == 0 else (3 if b in (1, 2) else S.END)

        trace_left_orig = S.run(tree, choose_left, 100)
        trace_left_simp = S.run(simplified, choose_left, 100)
        self.assertEqual(trace_left_orig, trace_left_simp)
        self.assertEqual(trace_left_simp, [0, 1, 3])

        trace_right_orig = S.run(tree, choose_right, 100)
        trace_right_simp = S.run(simplified, choose_right, 100)
        self.assertEqual(trace_right_orig, trace_right_simp)
        self.assertEqual(trace_right_simp, [0, 2, 3])

    def test_dead_branch_pruned_ast_execution(self):
        succ = [[1, 2], [3], [3], []]
        tree = S.structure(succ)
        self.assertIsNotNone(tree)

        simplified = simplify_ast(tree, reachable={0, 1, 3})
        blocks = collect_ast_blocks(simplified)
        self.assertNotIn(2, blocks)
        self.assertEqual(blocks, {0, 1, 3})

        def choose_path(b):
            return 1 if b == 0 else (3 if b == 1 else S.END)

        trace = S.run(simplified, choose_path, 100)
        self.assertEqual(trace, [0, 1, 3])


class PipelineExecutionTest(unittest.TestCase):
    """Test full CLI pipeline execution with synthetic shaders and mocks."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self.tmp.name, "repo")
        self.shaders = os.path.join(self.tmp.name, "shaders")
        self.out = os.path.join(self.tmp.name, "out")
        os.makedirs(self.repo)
        os.makedirs(os.path.join(self.shaders, "dxbc", "disasm"))
        os.makedirs(os.path.join(self.shaders, "spirv"))
        os.makedirs(os.path.join(self.repo, "runtime", "build"))

    def tearDown(self):
        self.tmp.cleanup()

    @patch("subprocess.run")
    def test_main_cli_pipeline_success(self, mock_run) -> None:
        self.assert_pipeline_outcome(mock_run, 64 * 64 * 16, "identical")

    @patch("subprocess.run")
    def test_main_rejects_identical_truncated_images(self, mock_run) -> None:
        self.assert_pipeline_outcome(mock_run, 16, "dispatch form failed to run")

    def assert_pipeline_outcome(self, mock_run, image_size: int, expected: str) -> None:
        import json

        # Prepare synthetic index
        index = {
            "ps_test": {
                "stage": "ps",
                "bindings": [0, 1],
                "features": ["bufferDeviceAddress"],
            }
        }
        with open(os.path.join(self.shaders, "spirv", "index.json"), "w", encoding="utf-8") as f:
            json.dump(index, f)

        # Prepare disassembly and placeholder spirv
        disasm_path = os.path.join(self.shaders, "dxbc", "disasm", "ps_test.s")
        with open(disasm_path, "w", encoding="utf-8") as f:
            f.write("\ts_cbranch_scc0 10\n\ts_branch 20\n\ts_endpgm\n")

        with open(os.path.join(self.shaders, "spirv", "ps_test.spv"), "wb") as f:
            f.write(b"placeholder")

        # Mock runner behavior:
        # 1. spirv-as -> returns 0
        # 2. spirv-val -> returns 0
        # 3. rdna2spirv new -> returns "1 pass spirv-val"
        # 4. rdna2spirv dispatch -> returns "1 pass spirv-val"
        # 5. vk_texture_test harness run 1 -> creates raw image
        # 6. vk_texture_test harness run 2 -> creates raw image
        def side_effect(args, **kwargs):
            cmd = args[0]
            if "spirv-as" in cmd:
                with open(args[-1], "wb") as f:
                    f.write(b"fake_vs_spv")
                return SimpleNamespace(returncode=0, stdout="pass")
            elif "spirv-val" in cmd:
                return SimpleNamespace(returncode=0, stdout="pass")
            elif "rdna2spirv.py" in str(args):
                # Write placeholder spvasm with %out_mrt0
                out_dir = args[args.index("--out") + 1]
                os.makedirs(out_dir, exist_ok=True)
                with open(os.path.join(out_dir, "ps_test.spvasm"), "w", encoding="utf-8") as f:
                    f.write("OpCapability Shader\n%out_mrt0 = OpVariable %po Output\n")
                with open(os.path.join(out_dir, "ps_test.spv"), "wb") as f:
                    f.write(b"fake_spv")
                return SimpleNamespace(returncode=0, stdout="1 pass spirv-val\n")
            else:
                # Harness call
                raw_path = args[3]
                with open(raw_path, "wb") as f:
                    f.write(bytes(image_size))
                return SimpleNamespace(returncode=0, stdout="OK")

        mock_run.side_effect = side_effect

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            with contextlib.redirect_stdout(io.StringIO()):
                ret = AB.main([
                    self.repo,
                    self.shaders,
                    self.out,
                    "--count", "1",
                    "--runs", "1",
                    "--harness", "dummy_harness.exe",
                ])
        self.assertEqual(ret, 0)
        outcomes_file = os.path.join(self.out, "outcomes.tsv")
        self.assertTrue(os.path.isfile(outcomes_file))
        with open(outcomes_file, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertEqual(content, f"ps_test\t{expected}\n")


if __name__ == "__main__":
    unittest.main()
