"""Semantic interpreter tests for signed 32-bit LDS atomic maximum (ds_max_i32, ds_max_rtn_i32).

Standard library only; no GPU, no LLVM, no external tools, no game files.

    python -I tests/semantic/test_lds_atomic_max_i32.py
    python -m unittest discover -s tests/semantic -p test_lds_atomic_max_i32.py

Background & Specification Rules:
  * AMD RDNA2 / GFX10 Local Data Share (LDS) Architecture:
    - LDS is an on-chip software-managed scratchpad memory shared across all threads
      in a workgroup (threadgroup).
    - `ds_max_i32 vaddr, vdata [offset:N]`:
        Atomically computes:
          addr = (vaddr + byte_offset)
          idx = (addr >> 2) & (lds_dwords - 1)
          LDS[idx] = max_i32(LDS[idx], vdata)
        Non-returning form: the prior value at LDS[idx] is discarded.
    - `ds_max_rtn_i32 vdst, vaddr, vdata [offset:N]`:
        Atomically computes:
          addr = (vaddr + byte_offset)
          idx = (addr >> 2) & (lds_dwords - 1)
          old_val = LDS[idx]
          LDS[idx] = max_i32(old_val, vdata)
          vdst = old_val
        Returning form: writes prior value into destination VGPR vdst.
    - Signed Two's-Complement 32-bit Arithmetic:
        Operands are interpreted as signed 32-bit integers in [-2^31, 2^31 - 1].
        For example:
          -1 (0xFFFFFFFF) is greater than -2 (0xFFFFFFFE).
          0 (0x00000000) is greater than all negative values (e.g. 0 > 0xFFFFFFFF).
          Positive values (e.g. 1) are greater than 0 and all negative values.
        Crucially diverges from unsigned maximum (ds_max_u32 / OpAtomicUMax) where
        0xFFFFFFFF (4294967295) is the maximal value.
    - Active lane masking:
        LDS operations are vector instructions; only active lanes participate
        under execution mask control (%exec_active).
    - Hardware wrapping & alignment:
        Byte addresses are converted to dwords by shifting right by 2 (truncating
        unaligned low bits). The dword index is masked with (lds_dwords - 1),
        wrapping out-of-bounds addresses within the allocated power-of-two LDS slice.
  * SPIR-V Representation:
    - LDS is declared as an array in the Workgroup storage class:
        %lds_arr = OpTypeArray %uint <lds_dwords>
        %_ptr_WG_lds = OpTypePointer Workgroup %lds_arr
        %_ptr_WG_uint = OpTypePointer Workgroup %uint
        %lds = OpVariable %_ptr_WG_lds Workgroup
    - Signed atomic maximum maps directly to OpAtomicSMax:
        %old = OpAtomicSMax %uint %ptr %scope %semantics %val
        where %scope is Workgroup (2) and %semantics is Relaxed / None (0).
"""
from __future__ import annotations

import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TOOLS_DIR = os.path.join(ROOT, "tools")
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

import rdna2spirv  # noqa: E402

MASK = 0xFFFFFFFF
SIGN_BIT = 0x80000000
INT32_MIN_SIGNED = -0x80000000
INT32_MAX_SIGNED = 0x7FFFFFFF
INT32_MIN = 0x80000000
INT32_MAX = 0x7FFFFFFF


class UnsupportedConstruct(NotImplementedError):
    """The module uses an unmodelled SPIR-V opcode, type, or storage class."""


class WorkgroupBoundsError(ValueError):
    """Workgroup dimension or memory bounds violation."""


_IGNORED = frozenset({
    "OpCapability", "OpExtInstImport", "OpMemoryModel", "OpEntryPoint", "OpExecutionMode",
    "OpDecorate", "OpMemberDecorate", "OpTypeVoid", "OpTypeFunction",
    "OpReturn", "OpFunctionEnd", "OpName", "OpSource", "OpSelectionMerge",
})

_LINE = re.compile(r"^(?:(%\S+)\s*=\s*)?(Op\w+)\s*(.*)$")


def to_signed_i32(val: int) -> int:
    """Converts a 32-bit unsigned integer to signed two's-complement integer."""
    val &= MASK
    return val - (1 << 32) if (val & SIGN_BIT) else val


def to_unsigned_u32(val: int) -> int:
    """Converts an integer to 32-bit unsigned representation."""
    return val & MASK


def reference_atomic_max_i32(initial: int, data: int) -> int:
    """Reference signed 32-bit two's complement atomic maximum."""
    s_init = to_signed_i32(initial)
    s_data = to_signed_i32(data)
    return to_unsigned_u32(max(s_init, s_data))


def reference_lds_index(base_addr: int, byte_offset: int, lds_dwords: int) -> int:
    """Reference dword index calculation with hardware wrapping."""
    effective_byte = (base_addr + byte_offset) & MASK
    dword_idx = effective_byte >> 2
    return dword_idx & (lds_dwords - 1)


class LdsAtomicMaxI32Interp:
    """Strict semantic interpreter for compute SPIR-V modules exercising LDS signed atomic maximum."""

    def __init__(self, spvasm: str):
        self.spvasm = spvasm
        self.types: dict[str, tuple] = {}
        self.constants: dict[str, int | bool] = {}
        self.ptr_types: dict[str, tuple[str, str]] = {}
        self.var_class: dict[str, str] = {}
        self.local_size: tuple[int, int, int] = (1, 1, 1)
        self.lds_var: str | None = None
        self.lds_size: int = 0
        self.body: list[tuple[str | None, str, list[str], str]] = []
        self.label_indices: dict[str, int] = {}
        self._parse()

    def _parse(self) -> None:
        in_body = False
        for raw in self.spvasm.splitlines():
            line = raw.strip()
            if not line:
                continue
            m = _LINE.match(line)
            if not m:
                raise UnsupportedConstruct(f"Cannot parse line: {line!r}")
            res, op, rest = m.groups()
            a = rest.split()
            if in_body:
                if op == "OpLabel" and res:
                    self.label_indices[res] = len(self.body)
                self.body.append((res, op, a, line))
                continue
            if op == "OpFunction":
                in_body = True
                continue
            if op == "OpExecutionMode" and len(a) >= 5 and a[1] == "LocalSize":
                lx, ly, lz = int(a[2]), int(a[3]), int(a[4])
                if lx < 1 or ly < 1 or lz < 1 or (lx * ly * lz) > 1024:
                    raise WorkgroupBoundsError(f"Invalid LocalSize {lx}x{ly}x{lz}")
                self.local_size = (lx, ly, lz)
                continue
            if op in _IGNORED:
                continue
            if op == "OpTypeInt":
                if int(a[0]) != 32:
                    raise UnsupportedConstruct("Only 32-bit int supported")
                self.types[res] = ("int", int(a[0]), int(a[1]))
            elif op == "OpTypeBool":
                self.types[res] = ("bool",)
            elif op == "OpTypeVector":
                self.types[res] = ("vec", a[0], int(a[1]))
            elif op == "OpTypeArray":
                size = int(self.constants[a[1]])
                self.types[res] = ("arr", a[0], size)
            elif op == "OpTypePointer":
                self.ptr_types[res] = (a[0], a[1])
            elif op == "OpConstant":
                self.constants[res] = int(a[1], 0) & MASK
            elif op == "OpConstantTrue":
                self.constants[res] = True
            elif op == "OpConstantFalse":
                self.constants[res] = False
            elif op == "OpVariable":
                cls, pointee = self.ptr_types[a[0]]
                if a[1] != cls:
                    raise UnsupportedConstruct("OpVariable storage class mismatch")
                self.var_class[res] = cls
                if cls == "Workgroup":
                    if self.lds_var is not None:
                        raise UnsupportedConstruct("Multiple Workgroup variables")
                    self.lds_var = res
                    arr_ty = self.types[pointee]
                    if arr_ty[0] != "arr":
                        raise UnsupportedConstruct("Workgroup variable must be an array")
                    self.lds_size = arr_ty[2]
                elif cls not in ("Output", "Function"):
                    raise UnsupportedConstruct(f"Unsupported storage class {cls}")
            else:
                raise UnsupportedConstruct(f"Header opcode {op}")

    def run(
        self,
        num_invocations: int | None = None,
        seed_lds: dict[int, int] | None = None,
        lane_inputs: list[dict[str, int | bool]] | None = None,
        exec_mask: int | None = None,
        lane_order: list[int] | None = None,
    ) -> tuple[list[int], list[dict[str, int]]]:
        """Executes the workgroup and returns (final_lds, outputs_per_lane)."""
        lx, ly, lz = self.local_size
        total_invocations = lx * ly * lz
        count = total_invocations if num_invocations is None else min(num_invocations, total_invocations)

        lds = [0] * self.lds_size
        if seed_lds:
            for idx, val in seed_lds.items():
                if 0 <= idx < self.lds_size:
                    lds[idx] = val & MASK

        order = lane_order if lane_order is not None else list(range(count))
        lane_outputs: list[dict[str, int] | None] = [None] * count

        for lidx in order:
            if lidx >= count:
                continue
            inputs = dict(lane_inputs[lidx]) if lane_inputs and lidx < len(lane_inputs) else {}
            if exec_mask is not None:
                inputs["%exec_active"] = bool((exec_mask >> lidx) & 1)
            out = self._run_lane(lidx, lds, inputs)
            lane_outputs[lidx] = out

        resolved_outputs = [o if o is not None else {} for o in lane_outputs]
        return list(lds), resolved_outputs

    def _run_lane(self, lidx: int, lds: list[int], inputs: dict[str, int | bool]) -> dict[str, int]:
        values: dict[str, int | bool] = dict(self.constants)
        values.update(inputs)
        var_box: dict[str, list[int]] = {}
        pointers: dict[str, tuple[str, list[int], int | None]] = {}
        outputs: list[str] = []

        if self.lds_var:
            pointers[self.lds_var] = ("lds", lds, None)

        for res, cls in self.var_class.items():
            if cls == "Output":
                var_box[res] = [0]
                pointers[res] = ("out", var_box[res], 0)
                outputs.append(res)
            elif cls == "Function":
                var_box[res] = [0]
                pointers[res] = ("fn", var_box[res], 0)

        def val(x: str) -> int | bool:
            if x in values:
                return values[x]
            raise UnsupportedConstruct(f"Undefined identifier {x}")

        pc = 0
        total_insns = len(self.body)
        while pc < total_insns:
            res, op, a, _ = self.body[pc]
            pc += 1

            if op in _IGNORED or op == "OpLabel":
                continue
            if op == "OpReturn":
                break
            if op == "OpBranch":
                target = a[0]
                if target not in self.label_indices:
                    raise UnsupportedConstruct(f"Unknown branch label {target}")
                pc = self.label_indices[target]
                continue
            if op == "OpBranchConditional":
                cond = bool(val(a[0]))
                target = a[1] if cond else a[2]
                if target not in self.label_indices:
                    raise UnsupportedConstruct(f"Unknown branch target {target}")
                pc = self.label_indices[target]
                continue
            if op == "OpVariable":
                cls, _ = self.ptr_types[a[0]]
                if cls != "Function":
                    raise UnsupportedConstruct("Non-Function variable in body")
                var_box[res] = [int(val(a[2])) if len(a) > 2 else 0]
                pointers[res] = ("fn", var_box[res], 0)
            elif op == "OpAccessChain":
                base = a[1]
                if base not in pointers:
                    raise UnsupportedConstruct(f"OpAccessChain on non-pointer {base}")
                ptag = pointers[base][0]
                if ptag == "lds":
                    idx = int(val(a[2]))
                    if not (0 <= idx < len(lds)):
                        raise WorkgroupBoundsError(f"LDS access out of bounds: {idx} >= {len(lds)}")
                    pointers[res] = ("lds", lds, idx)
                elif ptag in ("out", "fn"):
                    pointers[res] = pointers[base]
                else:
                    raise UnsupportedConstruct(f"Access chain on {ptag}")
            elif op == "OpLoad":
                ptr = pointers[a[1]]
                ptag = ptr[0]
                if ptag == "lds":
                    values[res] = ptr[1][ptr[2]] & MASK
                elif ptag in ("out", "fn"):
                    values[res] = ptr[1][ptr[2]] & MASK
            elif op == "OpStore":
                ptr = pointers[a[0]]
                v = int(val(a[1])) & MASK
                ptag = ptr[0]
                if ptag == "lds":
                    ptr[1][ptr[2]] = v
                elif ptag in ("out", "fn"):
                    ptr[1][ptr[2]] = v
            elif op == "OpAtomicSMax":
                ptr = pointers[a[1]]
                ptag = ptr[0]
                if ptag != "lds":
                    raise UnsupportedConstruct(f"OpAtomicSMax on non-LDS pointer {ptag}")
                data = int(val(a[4])) & MASK
                old = ptr[1][ptr[2]] & MASK
                ptr[1][ptr[2]] = reference_atomic_max_i32(old, data)
                values[res] = old
            elif op == "OpAtomicUMax":
                ptr = pointers[a[1]]
                ptag = ptr[0]
                if ptag != "lds":
                    raise UnsupportedConstruct(f"OpAtomicUMax on non-LDS pointer {ptag}")
                data = int(val(a[4])) & MASK
                old = ptr[1][ptr[2]] & MASK
                ptr[1][ptr[2]] = max(old, data) & MASK
                values[res] = old
            elif op == "OpIAdd":
                values[res] = (int(val(a[1])) + int(val(a[2]))) & MASK
            elif op == "OpISub":
                values[res] = (int(val(a[1])) - int(val(a[2]))) & MASK
            elif op == "OpShiftRightLogical":
                sh = int(val(a[2]))
                if sh >= 32:
                    raise UnsupportedConstruct("Shift >= 32")
                values[res] = (int(val(a[1])) >> sh) & MASK
            elif op == "OpShiftLeftLogical":
                sh = int(val(a[2]))
                if sh >= 32:
                    raise UnsupportedConstruct("Shift >= 32")
                values[res] = (int(val(a[1])) << sh) & MASK
            elif op == "OpBitwiseAnd":
                values[res] = int(val(a[1])) & int(val(a[2])) & MASK
            elif op == "OpBitwiseOr":
                values[res] = (int(val(a[1])) | int(val(a[2]))) & MASK
            elif op == "OpBitwiseXor":
                values[res] = (int(val(a[1])) ^ int(val(a[2]))) & MASK
            elif op == "OpSelect":
                cond = bool(val(a[1]))
                values[res] = val(a[2]) if cond else val(a[3])
            elif op == "OpCopyObject":
                values[res] = val(a[1])
            elif op == "OpBitcast":
                values[res] = int(val(a[1])) & MASK
            else:
                raise UnsupportedConstruct(f"Opcode {op} not modelled in body")

        return {name: var_box[name][0] & MASK for name in outputs}


class TestAtomicMaxI32ReferenceMath(unittest.TestCase):
    """Verifies signed 32-bit two's complement maximum semantics and unsigned divergence."""

    def test_basic_positive_comparisons(self):
        self.assertEqual(reference_atomic_max_i32(10, 20), 20)
        self.assertEqual(reference_atomic_max_i32(20, 10), 20)
        self.assertEqual(reference_atomic_max_i32(0, 0), 0)
        self.assertEqual(reference_atomic_max_i32(0, 100), 100)
        self.assertEqual(reference_atomic_max_i32(100, 0), 100)

    def test_zero_and_negative_comparisons(self):
        # 0 is strictly greater than any negative two's-complement integer
        cases = [
            (0, 0xFFFFFFFF, 0),  # 0 vs -1
            (0xFFFFFFFF, 0, 0),
            (0, 0xFFFFFFFB, 0),  # 0 vs -5
            (0xFFFFFFFB, 0, 0),
            (0, 0x80000000, 0),  # 0 vs -2147483648
            (0x80000000, 0, 0),
        ]
        for initial, data, expected in cases:
            with self.subTest(initial=hex(initial), data=hex(data)):
                self.assertEqual(reference_atomic_max_i32(initial, data), expected)

    def test_both_negative_comparisons(self):
        # -1 (0xFFFFFFFF) > -2 (0xFFFFFFFE) > -10 (0xFFFFFFF6) > -100 (0xFFFFFF9C)
        cases = [
            (0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFF),  # -1 vs -2 -> -1
            (0xFFFFFFFE, 0xFFFFFFFF, 0xFFFFFFFF),
            (0xFFFFFFF6, 0xFFFFFF9C, 0xFFFFFFF6),  # -10 vs -100 -> -10
            (0xFFFFFF9C, 0xFFFFFFF6, 0xFFFFFFF6),
            (0x80000000, 0xFFFFFFFF, 0xFFFFFFFF),  # INT32_MIN vs -1 -> -1
            (0xFFFFFFFF, 0x80000000, 0xFFFFFFFF),
        ]
        for initial, data, expected in cases:
            with self.subTest(initial=hex(initial), data=hex(data)):
                self.assertEqual(reference_atomic_max_i32(initial, data), expected)

    def test_positive_and_negative_comparisons(self):
        # Any positive value is greater than any negative value
        cases = [
            (1, 0xFFFFFFFF, 1),  # +1 vs -1 -> +1
            (0xFFFFFFFF, 1, 1),
            (42, 0x80000000, 42),  # +42 vs INT32_MIN -> +42
            (0x80000000, 42, 42),
            (0x7FFFFFFF, 0xFFFFFFFF, 0x7FFFFFFF),  # INT32_MAX vs -1 -> INT32_MAX
        ]
        for initial, data, expected in cases:
            with self.subTest(initial=hex(initial), data=hex(data)):
                self.assertEqual(reference_atomic_max_i32(initial, data), expected)

    def test_identical_values(self):
        for val in (0, 1, 0x7FFFFFFF, 0x80000000, 0xFFFFFFFF, 0x12345678, 0xFEDCBA98):
            with self.subTest(val=hex(val)):
                self.assertEqual(reference_atomic_max_i32(val, val), val)

    def test_signed_vs_unsigned_divergence(self):
        # Contrast signed max (OpAtomicSMax) with unsigned max (OpAtomicUMax)
        divergences = [
            # (val_a, val_b, expected_signed_max, expected_unsigned_max)
            (0xFFFFFFFF, 1, 1, 0xFFFFFFFF),  # -1 vs 1: signed=1, unsigned=4294967295
            (0x80000000, 0, 0, 0x80000000),  # INT32_MIN vs 0: signed=0, unsigned=2147483648
            (0x80000000, 0x7FFFFFFF, 0x7FFFFFFF, 0x80000000),  # INT32_MIN vs INT32_MAX: signed=INT32_MAX, unsigned=0x80000000
            (0xFFFFFFFE, 10, 10, 0xFFFFFFFE),  # -2 vs 10: signed=10, unsigned=0xFFFFFFFE
        ]
        for a, b, exp_signed, exp_unsigned in divergences:
            with self.subTest(a=hex(a), b=hex(b)):
                self.assertEqual(reference_atomic_max_i32(a, b), exp_signed)
                self.assertEqual(max(a, b), exp_unsigned)
                self.assertNotEqual(exp_signed, exp_unsigned)


class TestLdsAddressingAndOffsets(unittest.TestCase):
    """Verifies byte offset math, dword truncation, and power-of-two modulo wrapping."""

    def test_aligned_dword_addressing(self):
        lds_dwords = 256
        for dword in (0, 1, 2, 64, 127, 255):
            byte_addr = dword * 4
            self.assertEqual(reference_lds_index(byte_addr, 0, lds_dwords), dword)

    def test_unaligned_byte_truncation(self):
        lds_dwords = 128
        for byte_off in (0, 1, 2, 3):
            self.assertEqual(reference_lds_index(16, byte_off, lds_dwords), 4)
        for unaligned_base in (16, 17, 18, 19):
            self.assertEqual(reference_lds_index(unaligned_base, 0, lds_dwords), 4)

    def test_static_offset_addition(self):
        lds_dwords = 512
        base = 32  # dword 8
        offset = 64  # +16 dwords
        self.assertEqual(reference_lds_index(base, offset, lds_dwords), 24)

    def test_boundary_wrapping(self):
        lds_dwords = 64  # 256 bytes
        self.assertEqual(reference_lds_index(256, 0, lds_dwords), 0)
        self.assertEqual(reference_lds_index(256, 4, lds_dwords), 1)
        self.assertEqual(reference_lds_index(0xFFFFFFFC, 0, lds_dwords), 63)


class TestSpirvAtomicMaxI32NonReturning(unittest.TestCase):
    """Executes SPIR-V compute kernels with non-returning LDS signed atomic maximum."""

    def test_ds_max_i32_negative_to_positive(self):
        spvasm = """
OpCapability Shader
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main"
OpExecutionMode %main LocalSize 1 1 1
%uint = OpTypeInt 32 0
%c64 = OpConstant %uint 64
%c2 = OpConstant %uint 2
%c0 = OpConstant %uint 0
%c3 = OpConstant %uint 3
%c_pos = OpConstant %uint 25
%lds_arr = OpTypeArray %uint %c64
%_ptr_WG_lds = OpTypePointer Workgroup %lds_arr
%_ptr_WG_uint = OpTypePointer Workgroup %uint
%lds = OpVariable %_ptr_WG_lds Workgroup
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
%ptr = OpAccessChain %_ptr_WG_uint %lds %c3
%dummy = OpAtomicSMax %uint %ptr %c2 %c0 %c_pos
OpReturn
OpFunctionEnd
"""
        interp = LdsAtomicMaxI32Interp(spvasm)
        final_lds, _ = interp.run(seed_lds={3: 0xFFFFFF9C})  # -100
        self.assertEqual(final_lds[3], 25)
        self.assertEqual(final_lds[0], 0)
        self.assertEqual(final_lds[2], 0)
        self.assertEqual(final_lds[4], 0)

    def test_ds_max_i32_keeps_higher_existing(self):
        spvasm = """
OpCapability Shader
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main"
OpExecutionMode %main LocalSize 1 1 1
%uint = OpTypeInt 32 0
%c16 = OpConstant %uint 16
%c2 = OpConstant %uint 2
%c0 = OpConstant %uint 0
%c5 = OpConstant %uint 5
%c_neg = OpConstant %uint 4294967276
%lds_arr = OpTypeArray %uint %c16
%_ptr_WG_lds = OpTypePointer Workgroup %lds_arr
%_ptr_WG_uint = OpTypePointer Workgroup %uint
%lds = OpVariable %_ptr_WG_lds Workgroup
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
%ptr = OpAccessChain %_ptr_WG_uint %lds %c5
%dummy = OpAtomicSMax %uint %ptr %c2 %c0 %c_neg
OpReturn
OpFunctionEnd
"""
        interp = LdsAtomicMaxI32Interp(spvasm)
        final_lds, _ = interp.run(seed_lds={5: 100})
        self.assertEqual(final_lds[5], 100)

    def test_ds_max_i32_both_negative(self):
        spvasm = """
OpCapability Shader
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main"
OpExecutionMode %main LocalSize 1 1 1
%uint = OpTypeInt 32 0
%c16 = OpConstant %uint 16
%c2 = OpConstant %uint 2
%c0 = OpConstant %uint 0
%c2_slot = OpConstant %uint 2
%c_minus10 = OpConstant %uint 4294967286
%lds_arr = OpTypeArray %uint %c16
%_ptr_WG_lds = OpTypePointer Workgroup %lds_arr
%_ptr_WG_uint = OpTypePointer Workgroup %uint
%lds = OpVariable %_ptr_WG_lds Workgroup
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
%ptr = OpAccessChain %_ptr_WG_uint %lds %c2_slot
%dummy = OpAtomicSMax %uint %ptr %c2 %c0 %c_minus10
OpReturn
OpFunctionEnd
"""
        interp = LdsAtomicMaxI32Interp(spvasm)
        final_lds, _ = interp.run(seed_lds={2: 0xFFFFFF9C})  # -100 vs -10 -> -10 (0xFFFFFFF6)
        self.assertEqual(final_lds[2], 0xFFFFFFF6)

    def test_ds_max_i32_with_exec_mask_inactive_skips(self):
        spvasm = """
OpCapability Shader
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main"
OpExecutionMode %main LocalSize 1 1 1
%uint = OpTypeInt 32 0
%bool = OpTypeBool
%c16 = OpConstant %uint 16
%c2 = OpConstant %uint 2
%c0 = OpConstant %uint 0
%c1 = OpConstant %uint 1
%c500 = OpConstant %uint 500
%lds_arr = OpTypeArray %uint %c16
%_ptr_WG_lds = OpTypePointer Workgroup %lds_arr
%_ptr_WG_uint = OpTypePointer Workgroup %uint
%lds = OpVariable %_ptr_WG_lds Workgroup
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
%m_end = OpLabel
%m_do = OpLabel
OpSelectionMerge %m_end None
OpBranchConditional %exec_active %m_do %m_end
%m_do = OpLabel
%ptr = OpAccessChain %_ptr_WG_uint %lds %c1
%dummy = OpAtomicSMax %uint %ptr %c2 %c0 %c500
OpBranch %m_end
%m_end = OpLabel
OpReturn
OpFunctionEnd
"""
        interp = LdsAtomicMaxI32Interp(spvasm)
        # When exec_active is 0, lane must skip LDS write
        final_lds, _ = interp.run(seed_lds={1: 10}, exec_mask=0)
        self.assertEqual(final_lds[1], 10)

        # When exec_active is 1, lane modifies LDS
        final_lds2, _ = interp.run(seed_lds={1: 10}, exec_mask=1)
        self.assertEqual(final_lds2[1], 500)


class TestSpirvAtomicMaxI32Returning(unittest.TestCase):
    """Executes SPIR-V compute kernels with returning LDS signed atomic maximum."""

    def test_ds_max_rtn_i32_returns_old_val_and_updates_memory(self):
        spvasm = """
OpCapability Shader
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main" %out
OpExecutionMode %main LocalSize 1 1 1
%uint = OpTypeInt 32 0
%_ptr_Output_uint = OpTypePointer Output %uint
%out = OpVariable %_ptr_Output_uint Output
%c64 = OpConstant %uint 64
%c2 = OpConstant %uint 2
%c0 = OpConstant %uint 0
%c7 = OpConstant %uint 7
%c50 = OpConstant %uint 50
%lds_arr = OpTypeArray %uint %c64
%_ptr_WG_lds = OpTypePointer Workgroup %lds_arr
%_ptr_WG_uint = OpTypePointer Workgroup %uint
%lds = OpVariable %_ptr_WG_lds Workgroup
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
%ptr = OpAccessChain %_ptr_WG_uint %lds %c7
%old = OpAtomicSMax %uint %ptr %c2 %c0 %c50
OpStore %out %old
OpReturn
OpFunctionEnd
"""
        interp = LdsAtomicMaxI32Interp(spvasm)
        initial_val = 0xFFFFFF9C  # -100
        final_lds, lane_out = interp.run(seed_lds={7: initial_val})
        # Output register must contain the prior value (-100)
        self.assertEqual(lane_out[0]["%out"], initial_val)
        # LDS slot must contain max(-100, 50) = 50
        self.assertEqual(final_lds[7], 50)

    def test_ds_max_rtn_i32_when_input_is_smaller(self):
        spvasm = """
OpCapability Shader
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main" %out
OpExecutionMode %main LocalSize 1 1 1
%uint = OpTypeInt 32 0
%_ptr_Output_uint = OpTypePointer Output %uint
%out = OpVariable %_ptr_Output_uint Output
%c16 = OpConstant %uint 16
%c2 = OpConstant %uint 2
%c0 = OpConstant %uint 0
%c3 = OpConstant %uint 3
%c_minus5 = OpConstant %uint 4294967291
%lds_arr = OpTypeArray %uint %c16
%_ptr_WG_lds = OpTypePointer Workgroup %lds_arr
%_ptr_WG_uint = OpTypePointer Workgroup %uint
%lds = OpVariable %_ptr_WG_lds Workgroup
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
%ptr = OpAccessChain %_ptr_WG_uint %lds %c3
%old = OpAtomicSMax %uint %ptr %c2 %c0 %c_minus5
OpStore %out %old
OpReturn
OpFunctionEnd
"""
        interp = LdsAtomicMaxI32Interp(spvasm)
        initial_val = 15
        final_lds, lane_out = interp.run(seed_lds={3: initial_val})
        # Prior value is returned
        self.assertEqual(lane_out[0]["%out"], initial_val)
        # Memory is unchanged because 15 > -5
        self.assertEqual(final_lds[3], 15)

    def test_chained_atomic_max_same_slot(self):
        spvasm = """
OpCapability Shader
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main" %out
OpExecutionMode %main LocalSize 1 1 1
%uint = OpTypeInt 32 0
%_ptr_Output_uint = OpTypePointer Output %uint
%out = OpVariable %_ptr_Output_uint Output
%c16 = OpConstant %uint 16
%c2 = OpConstant %uint 2
%c0 = OpConstant %uint 0
%c4_slot = OpConstant %uint 4
%c_minus20 = OpConstant %uint 4294967276
%c_0 = OpConstant %uint 0
%c_pos100 = OpConstant %uint 100
%lds_arr = OpTypeArray %uint %c16
%_ptr_WG_lds = OpTypePointer Workgroup %lds_arr
%_ptr_WG_uint = OpTypePointer Workgroup %uint
%lds = OpVariable %_ptr_WG_lds Workgroup
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
%ptr = OpAccessChain %_ptr_WG_uint %lds %c4_slot
%r1 = OpAtomicSMax %uint %ptr %c2 %c0 %c_minus20
%r2 = OpAtomicSMax %uint %ptr %c2 %c0 %c_0
%r3 = OpAtomicSMax %uint %ptr %c2 %c0 %c_pos100
OpStore %out %r3
OpReturn
OpFunctionEnd
"""
        interp = LdsAtomicMaxI32Interp(spvasm)
        final_lds, lane_out = interp.run(seed_lds={4: 0xFFFFFFCE})  # seed = -50
        # r1 returned -50 (LDS became max(-50, -20) = -20)
        # r2 returned -20 (LDS became max(-20, 0) = 0)
        # r3 returned 0 (LDS became max(0, 100) = 100)
        self.assertEqual(lane_out[0]["%out"], 0)
        self.assertEqual(final_lds[4], 100)

    def test_offset_and_wrapping_in_spirv(self):
        # Simulates rdna2spirv sequence:
        # byte_addr + offset -> shift right 2 -> bitwise and mask -> OpAccessChain
        spvasm = """
OpCapability Shader
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main" %out
OpExecutionMode %main LocalSize 1 1 1
%uint = OpTypeInt 32 0
%_ptr_Output_uint = OpTypePointer Output %uint
%out = OpVariable %_ptr_Output_uint Output
%c16 = OpConstant %uint 16
%c15 = OpConstant %uint 15
%c2 = OpConstant %uint 2
%c0 = OpConstant %uint 0
%c_base = OpConstant %uint 60
%c_offset = OpConstant %uint 16
%c_val = OpConstant %uint 42
%lds_arr = OpTypeArray %uint %c16
%_ptr_WG_lds = OpTypePointer Workgroup %lds_arr
%_ptr_WG_uint = OpTypePointer Workgroup %uint
%lds = OpVariable %_ptr_WG_lds Workgroup
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
%s1 = OpIAdd %uint %c_base %c_offset
%i = OpShiftRightLogical %uint %s1 %c2
%m = OpBitwiseAnd %uint %i %c15
%ptr = OpAccessChain %_ptr_WG_uint %lds %m
%old = OpAtomicSMax %uint %ptr %c2 %c0 %c_val
OpStore %out %old
OpReturn
OpFunctionEnd
"""
        # (60 + 16) >> 2 = 19; 19 & 15 = 3
        interp = LdsAtomicMaxI32Interp(spvasm)
        final_lds, lane_out = interp.run(seed_lds={3: 0xFFFFFFFF})  # -1
        self.assertEqual(lane_out[0]["%out"], 0xFFFFFFFF)
        self.assertEqual(final_lds[3], 42)


class TestTranslatorMaxI32Integration(unittest.TestCase):
    """Verifies that tools/rdna2spirv.py emits conforming SPIR-V for ds_max_i32 and ds_max_rtn_i32."""

    def _make_mock_gen(self, stage: str = "cs"):
        class MockGen:
            def __init__(self):
                self.stage = stage
                self.uses_lds = False
                self.lds_dwords = 256
                self.lds_bound = None       # not bounds-checked (SHADER-309)
                self.wave64 = True
                self._tmp = 0
                self.emitted: list[str] = []
                self.stored_dsts: list[tuple[dict, str]] = []

            def tmp(self) -> str:
                self._tmp += 1
                return f"%t{self._tmp}"

            def cu(self, v: int) -> str:
                return f"%c{v}"

            def active_b(self) -> str:
                return "%exec_active"

            def src_u(self, arg: dict) -> str:
                return f"%v_{arg['r']}"

            def dst(self, d: dict, v: str) -> None:
                self.stored_dsts.append((d, v))

            def emit(self, line: str) -> None:
                self.emitted.append(line)

            LDS_ATOMICS = rdna2spirv.Gen.LDS_ATOMICS
            mod_int = staticmethod(rdna2spirv.Gen.mod_int)
            lds_ptr = rdna2spirv.Gen.lds_ptr
            lds_access = rdna2spirv.Gen.lds_access
            lds_atomic = rdna2spirv.Gen.lds_atomic
            lds_op = rdna2spirv.Gen.lds_op

        return MockGen()

    def test_lds_atomics_table_contains_ds_max_i32(self):
        self.assertIn("ds_max_i32", rdna2spirv.Gen.LDS_ATOMICS)
        self.assertEqual(rdna2spirv.Gen.LDS_ATOMICS["ds_max_i32"], "OpAtomicSMax")

    def test_ds_max_i32_non_returning_emission(self):
        gen = self._make_mock_gen(stage="cs")
        # ds_max_i32 v0, v1, offset:8 (args: addr=v0, data=v1)
        gen.lds_op("ds_max_i32", [{"r": 0}, {"r": 1}], ["offset:8"])
        self.assertTrue(gen.uses_lds)
        emitted_str = "\n".join(gen.emitted)

        self.assertIn("OpBranchConditional %exec_active", emitted_str)
        self.assertIn("OpIAdd %uint %v_0 %c8", emitted_str)
        self.assertIn("OpShiftRightLogical %uint", emitted_str)
        self.assertIn("OpBitwiseAnd %uint", emitted_str)
        self.assertIn("OpAccessChain %_ptr_WG_uint %lds", emitted_str)
        self.assertIn("OpAtomicSMax %uint", emitted_str)
        self.assertIn("%c2 %c0 %v_1", emitted_str)
        self.assertEqual(len(gen.stored_dsts), 0)

    def test_ds_max_rtn_i32_returning_emission(self):
        gen = self._make_mock_gen(stage="cs")
        # ds_max_rtn_i32 v5, v0, v1, offset:12 (args: dst=v5, addr=v0, data=v1)
        gen.lds_op("ds_max_rtn_i32", [{"r": 5}, {"r": 0}, {"r": 1}], ["offset:12"])
        self.assertTrue(gen.uses_lds)
        emitted_str = "\n".join(gen.emitted)

        self.assertIn("OpAtomicSMax %uint", emitted_str)
        self.assertIn("%c2 %c0 %v_1", emitted_str)
        self.assertEqual(len(gen.stored_dsts), 1)
        dest_reg, val_id = gen.stored_dsts[0]
        self.assertEqual(dest_reg["r"], 5)
        self.assertTrue(val_id.startswith("%t"))

    def test_ds_max_outside_compute_raises_unsupported(self):
        for invalid_stage in ("ps", "vs", "gs", "hs", "ds"):
            with self.subTest(stage=invalid_stage):
                gen = self._make_mock_gen(stage=invalid_stage)
                with self.assertRaises(rdna2spirv.Unsupported):
                    gen.lds_op("ds_max_i32", [{"r": 0}, {"r": 1}], [])


class TestSimdMultiLaneAddressClash(unittest.TestCase):
    """Verifies SIMD wavefront multi-lane concurrency, address clashes, and return ticket ordering."""

    def test_multi_lane_shared_address_clash_signed_reduction(self):
        # 32 lanes (a full wave) concurrently target LDS[0] with signed values spanning [-32, +30]
        spvasm = """
OpCapability Shader
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main" %out
OpExecutionMode %main LocalSize 32 1 1
%uint = OpTypeInt 32 0
%_ptr_Output_uint = OpTypePointer Output %uint
%out = OpVariable %_ptr_Output_uint Output
%c64 = OpConstant %uint 64
%c2 = OpConstant %uint 2
%c0 = OpConstant %uint 0
%c0_slot = OpConstant %uint 0
%lds_arr = OpTypeArray %uint %c64
%_ptr_WG_lds = OpTypePointer Workgroup %lds_arr
%_ptr_WG_uint = OpTypePointer Workgroup %uint
%lds = OpVariable %_ptr_WG_lds Workgroup
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
%ptr = OpAccessChain %_ptr_WG_uint %lds %c0_slot
%ticket = OpAtomicSMax %uint %ptr %c2 %c0 %v_data
OpStore %out %ticket
OpReturn
OpFunctionEnd
"""
        interp = LdsAtomicMaxI32Interp(spvasm)
        seed_val = 0xFFFFFF9C  # -100
        # Lane i inputs: -32, -30, -28, ..., +28, +30
        lane_inputs = [{"%v_data": to_unsigned_u32(-32 + 2 * i)} for i in range(32)]
        final_lds, lane_outs = interp.run(num_invocations=32, seed_lds={0: seed_val}, lane_inputs=lane_inputs)

        # Final value in memory must be the global signed maximum (+30)
        self.assertEqual(final_lds[0], 30)

        # Returned tickets: lane 0 sees -100, and subsequent lanes see non-decreasing signed values
        tickets = [to_signed_i32(o["%out"]) for o in lane_outs]
        self.assertEqual(len(tickets), 32)
        self.assertEqual(tickets[0], -100)
        for i in range(1, 32):
            self.assertGreaterEqual(tickets[i], tickets[i - 1])
        # The last lane sees +28 because all prior values were smaller
        self.assertEqual(tickets[-1], 28)

    def test_multi_lane_clash_all_negative_values(self):
        # 16 lanes all passing negative values to the same LDS slot
        spvasm = """
OpCapability Shader
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main" %out
OpExecutionMode %main LocalSize 16 1 1
%uint = OpTypeInt 32 0
%_ptr_Output_uint = OpTypePointer Output %uint
%out = OpVariable %_ptr_Output_uint Output
%c32 = OpConstant %uint 32
%c2 = OpConstant %uint 2
%c0 = OpConstant %uint 0
%c0_slot = OpConstant %uint 0
%lds_arr = OpTypeArray %uint %c32
%_ptr_WG_lds = OpTypePointer Workgroup %lds_arr
%_ptr_WG_uint = OpTypePointer Workgroup %uint
%lds = OpVariable %_ptr_WG_lds Workgroup
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
%ptr = OpAccessChain %_ptr_WG_uint %lds %c0_slot
%ticket = OpAtomicSMax %uint %ptr %c2 %c0 %v_data
OpStore %out %ticket
OpReturn
OpFunctionEnd
"""
        interp = LdsAtomicMaxI32Interp(spvasm)
        # Seed with -1000
        seed_val = to_unsigned_u32(-1000)
        # Lane i provides -500 + 10 * i (from -500 up to -350)
        lane_inputs = [{"%v_data": to_unsigned_u32(-500 + 10 * i)} for i in range(16)]
        final_lds, lane_outs = interp.run(num_invocations=16, seed_lds={0: seed_val}, lane_inputs=lane_inputs)

        # Signed maximum of negatives: -350 is the largest (closest to 0)
        self.assertEqual(final_lds[0], to_unsigned_u32(-350))
        tickets = [to_signed_i32(o["%out"]) for o in lane_outs]
        self.assertEqual(tickets[0], -1000)
        self.assertEqual(tickets[-1], -360)

    def test_multi_lane_clash_with_interleaved_extremes(self):
        # Even lanes pass large negative numbers; odd lanes pass small positive numbers
        spvasm = """
OpCapability Shader
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main" %out
OpExecutionMode %main LocalSize 32 1 1
%uint = OpTypeInt 32 0
%_ptr_Output_uint = OpTypePointer Output %uint
%out = OpVariable %_ptr_Output_uint Output
%c64 = OpConstant %uint 64
%c2 = OpConstant %uint 2
%c0 = OpConstant %uint 0
%c0_slot = OpConstant %uint 0
%lds_arr = OpTypeArray %uint %c64
%_ptr_WG_lds = OpTypePointer Workgroup %lds_arr
%_ptr_WG_uint = OpTypePointer Workgroup %uint
%lds = OpVariable %_ptr_WG_lds Workgroup
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
%ptr = OpAccessChain %_ptr_WG_uint %lds %c0_slot
%ticket = OpAtomicSMax %uint %ptr %c2 %c0 %v_data
OpStore %out %ticket
OpReturn
OpFunctionEnd
"""
        interp = LdsAtomicMaxI32Interp(spvasm)
        seed_val = to_unsigned_u32(-2000)
        lane_inputs = []
        for i in range(32):
            if i % 2 == 0:
                val = -10000 - i  # large negative
            else:
                val = 5 * i       # positive: 5, 15, ..., 155
            lane_inputs.append({"%v_data": to_unsigned_u32(val)})

        final_lds, lane_outs = interp.run(num_invocations=32, seed_lds={0: seed_val}, lane_inputs=lane_inputs)
        # Final maximum must be 5 * 31 = 155
        self.assertEqual(final_lds[0], 155)

        # Ensure that negative values never regress a previously stored higher value
        signed_mem = to_signed_i32(seed_val)
        for i, o in enumerate(lane_outs):
            ret_ticket = to_signed_i32(o["%out"])
            self.assertEqual(ret_ticket, signed_mem)
            input_val = to_signed_i32(lane_inputs[i]["%v_data"])
            signed_mem = max(signed_mem, input_val)

    def test_multi_lane_arrival_order_permutation_invariance(self):
        # 8 lanes with diverse signed values clashing on the same slot
        # Verifies that any arrival scheduling yields the identical final signed maximum
        spvasm = """
OpCapability Shader
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main"
OpExecutionMode %main LocalSize 8 1 1
%uint = OpTypeInt 32 0
%c16 = OpConstant %uint 16
%c2 = OpConstant %uint 2
%c0 = OpConstant %uint 0
%c0_slot = OpConstant %uint 0
%lds_arr = OpTypeArray %uint %c16
%_ptr_WG_lds = OpTypePointer Workgroup %lds_arr
%_ptr_WG_uint = OpTypePointer Workgroup %uint
%lds = OpVariable %_ptr_WG_lds Workgroup
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
%ptr = OpAccessChain %_ptr_WG_uint %lds %c0_slot
%dummy = OpAtomicSMax %uint %ptr %c2 %c0 %v_data
OpReturn
OpFunctionEnd
"""
        interp = LdsAtomicMaxI32Interp(spvasm)
        raw_vals = [-100, 50, -200, 300, 0, -1, 250, 10]
        lane_inputs = [{"%v_data": to_unsigned_u32(v)} for v in raw_vals]

        permutations = [
            list(range(8)),                  # Forward order
            list(reversed(range(8))),        # Reverse order
            [1, 3, 5, 7, 0, 2, 4, 6],        # Odd-then-even
            [7, 0, 6, 1, 5, 2, 4, 3],        # In-out weave
            [3, 0, 1, 2, 4, 5, 6, 7],        # Winner first
        ]
        expected_max = 300

        for order in permutations:
            with self.subTest(order=order):
                final_lds, _ = interp.run(
                    num_invocations=8,
                    seed_lds={0: to_unsigned_u32(-500)},
                    lane_inputs=lane_inputs,
                    lane_order=order,
                )
                self.assertEqual(final_lds[0], expected_max)

    def test_multi_lane_partial_active_mask_clash(self):
        # Only even lanes are active; odd lanes hold tempting large positive values but must not execute
        spvasm = """
OpCapability Shader
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main"
OpExecutionMode %main LocalSize 16 1 1
%uint = OpTypeInt 32 0
%bool = OpTypeBool
%c16 = OpConstant %uint 16
%c2 = OpConstant %uint 2
%c0 = OpConstant %uint 0
%c0_slot = OpConstant %uint 0
%lds_arr = OpTypeArray %uint %c16
%_ptr_WG_lds = OpTypePointer Workgroup %lds_arr
%_ptr_WG_uint = OpTypePointer Workgroup %uint
%lds = OpVariable %_ptr_WG_lds Workgroup
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
%m_end = OpLabel
%m_do = OpLabel
OpSelectionMerge %m_end None
OpBranchConditional %exec_active %m_do %m_end
%m_do = OpLabel
%ptr = OpAccessChain %_ptr_WG_uint %lds %c0_slot
%dummy = OpAtomicSMax %uint %ptr %c2 %c0 %v_data
OpBranch %m_end
%m_end = OpLabel
OpReturn
OpFunctionEnd
"""
        interp = LdsAtomicMaxI32Interp(spvasm)
        # Active even lanes: +10, +20, +30, ... (+80 max)
        # Inactive odd lanes: +9999 (must be ignored)
        lane_inputs = []
        for i in range(16):
            if i % 2 == 0:
                val = 10 * (i + 1)
            else:
                val = 9999
            lane_inputs.append({"%v_data": to_unsigned_u32(val)})

        # Exec mask: only even bits active: 0x5555 (bits 0, 2, 4, 6, 8, 10, 12, 14)
        exec_mask = 0x5555
        final_lds, _ = interp.run(num_invocations=16, seed_lds={0: 0}, lane_inputs=lane_inputs, exec_mask=exec_mask)
        # The maximal active value is 10 * 15 = 150 (for lane 14)
        self.assertEqual(final_lds[0], 150)


class TestLdsBankConflictsAndStridePatterns(unittest.TestCase):
    """Verifies 32-bank conflict patterns, stride strides, and workgroup tree reductions."""

    def test_32_lane_stride32_bank0_conflict(self):
        # 32 lanes each access dword (lane_id * 32), which all map to physical Bank 0 ((addr >> 2) % 32 == 0)
        # Each lane must independently write its signed maximum to its dedicated slot without interference
        spvasm = """
OpCapability Shader
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main" %out
OpExecutionMode %main LocalSize 32 1 1
%uint = OpTypeInt 32 0
%_ptr_Output_uint = OpTypePointer Output %uint
%out = OpVariable %_ptr_Output_uint Output
%c1024 = OpConstant %uint 1024
%c2 = OpConstant %uint 2
%c0 = OpConstant %uint 0
%lds_arr = OpTypeArray %uint %c1024
%_ptr_WG_lds = OpTypePointer Workgroup %lds_arr
%_ptr_WG_uint = OpTypePointer Workgroup %uint
%lds = OpVariable %_ptr_WG_lds Workgroup
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
%ptr = OpAccessChain %_ptr_WG_uint %lds %v_slot
%old = OpAtomicSMax %uint %ptr %c2 %c0 %v_data
OpStore %out %old
OpReturn
OpFunctionEnd
"""
        interp = LdsAtomicMaxI32Interp(spvasm)
        # Seed dwords 0, 32, 64, ..., 992 with negative seed (-50)
        seed_lds = {i * 32: to_unsigned_u32(-50) for i in range(32)}
        lane_inputs = []
        for i in range(32):
            slot = i * 32
            # Value varies: lane i passes signed (i - 16) -> spanning [-16, +15]
            val = to_unsigned_u32(i - 16)
            lane_inputs.append({"%v_slot": slot, "%v_data": val})

        final_lds, lane_outs = interp.run(num_invocations=32, seed_lds=seed_lds, lane_inputs=lane_inputs)

        for i in range(32):
            slot = i * 32
            # Each slot returned the seed (-50)
            self.assertEqual(lane_outs[i]["%out"], to_unsigned_u32(-50))
            # Memory contains max(-50, i - 16) = (i - 16)
            expected = to_unsigned_u32(i - 16)
            self.assertEqual(final_lds[slot], expected)

    def test_32_lane_conflict_free_diagonal(self):
        # 32 lanes each access dword i (Bank i % 32 == i) - fully conflict-free
        spvasm = """
OpCapability Shader
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main" %out
OpExecutionMode %main LocalSize 32 1 1
%uint = OpTypeInt 32 0
%_ptr_Output_uint = OpTypePointer Output %uint
%out = OpVariable %_ptr_Output_uint Output
%c64 = OpConstant %uint 64
%c2 = OpConstant %uint 2
%c0 = OpConstant %uint 0
%lds_arr = OpTypeArray %uint %c64
%_ptr_WG_lds = OpTypePointer Workgroup %lds_arr
%_ptr_WG_uint = OpTypePointer Workgroup %uint
%lds = OpVariable %_ptr_WG_lds Workgroup
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
%ptr = OpAccessChain %_ptr_WG_uint %lds %v_slot
%old = OpAtomicSMax %uint %ptr %c2 %c0 %v_data
OpStore %out %old
OpReturn
OpFunctionEnd
"""
        interp = LdsAtomicMaxI32Interp(spvasm)
        seed_lds = {i: to_unsigned_u32(-10 * (i + 1)) for i in range(32)}
        lane_inputs = []
        for i in range(32):
            # Input is greater than seed for each lane
            val = to_unsigned_u32(100 * (i + 1))
            lane_inputs.append({"%v_slot": i, "%v_data": val})

        final_lds, lane_outs = interp.run(num_invocations=32, seed_lds=seed_lds, lane_inputs=lane_inputs)

        for i in range(32):
            self.assertEqual(lane_outs[i]["%out"], seed_lds[i])
            self.assertEqual(final_lds[i], to_unsigned_u32(100 * (i + 1)))

    def test_multi_lane_2way_bank_conflict(self):
        # 32 lanes targeting 16 banks (2 lanes per bank: lanes k and k+16 target bank k at dword k and k+32)
        spvasm = """
OpCapability Shader
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main"
OpExecutionMode %main LocalSize 32 1 1
%uint = OpTypeInt 32 0
%c64 = OpConstant %uint 64
%c2 = OpConstant %uint 2
%c0 = OpConstant %uint 0
%lds_arr = OpTypeArray %uint %c64
%_ptr_WG_lds = OpTypePointer Workgroup %lds_arr
%_ptr_WG_uint = OpTypePointer Workgroup %uint
%lds = OpVariable %_ptr_WG_lds Workgroup
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
%ptr = OpAccessChain %_ptr_WG_uint %lds %v_slot
%dummy = OpAtomicSMax %uint %ptr %c2 %c0 %v_data
OpReturn
OpFunctionEnd
"""
        interp = LdsAtomicMaxI32Interp(spvasm)
        seed_lds = {}
        lane_inputs = []
        for i in range(32):
            bank = i % 16
            row = i // 16
            slot = bank + row * 32  # slots: 0..15 (bank 0..15) and 32..47 (bank 0..15)
            seed_lds[slot] = to_unsigned_u32(-500)
            lane_inputs.append({"%v_slot": slot, "%v_data": to_unsigned_u32(-100 + i * 10)})

        final_lds, _ = interp.run(num_invocations=32, seed_lds=seed_lds, lane_inputs=lane_inputs)
        for i in range(32):
            bank = i % 16
            row = i // 16
            slot = bank + row * 32
            self.assertEqual(final_lds[slot], to_unsigned_u32(-100 + i * 10))

    def test_workgroup_tournament_tree_reduction(self):
        # 32 elements reduced across workgroup tree passes into LDS[0]
        elements = [
            -150, 42, -999, 120, -5, 0, 77, -300,
            12, -88, 500, -1, 33, -40, 89, -12,
            -200, 310, -75, 4, -8, 99, -450, 16,
            64, -2, -33, 108, -19, 256, -60, 499
        ]
        self.assertEqual(len(elements), 32)
        true_max = max(elements)
        self.assertEqual(true_max, 500)

        # Step 1: 16 lanes reduce pairs [i] and [i + 16] using OpAtomicSMax
        # In a tournament, lane i loads elements[i + stride] and atomically maxes into elements[i]
        curr_lds = [to_unsigned_u32(x) for x in elements]

        stride = 16
        while stride >= 1:
            spvasm = f"""
OpCapability Shader
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main"
OpExecutionMode %main LocalSize {stride} 1 1
%uint = OpTypeInt 32 0
%c64 = OpConstant %uint 64
%c2 = OpConstant %uint 2
%c0 = OpConstant %uint 0
%lds_arr = OpTypeArray %uint %c64
%_ptr_WG_lds = OpTypePointer Workgroup %lds_arr
%_ptr_WG_uint = OpTypePointer Workgroup %uint
%lds = OpVariable %_ptr_WG_lds Workgroup
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
%ptr = OpAccessChain %_ptr_WG_uint %lds %v_dst_slot
%dummy = OpAtomicSMax %uint %ptr %c2 %c0 %v_src_val
OpReturn
OpFunctionEnd
"""
            interp = LdsAtomicMaxI32Interp(spvasm)
            seed = {idx: curr_lds[idx] for idx in range(len(curr_lds))}
            lane_inputs = []
            for lane in range(stride):
                lane_inputs.append({
                    "%v_dst_slot": lane,
                    "%v_src_val": curr_lds[lane + stride],
                })
            curr_lds, _ = interp.run(num_invocations=stride, seed_lds=seed, lane_inputs=lane_inputs)
            stride //= 2

        self.assertEqual(to_signed_i32(curr_lds[0]), true_max)

    def test_wave64_multi_lane_atomic_max_coordination(self):
        # 64 lanes (Wave64) coordinate: 64 lanes each target (lane_id % 4), so 16 lanes clash per slot
        spvasm = """
OpCapability Shader
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main"
OpExecutionMode %main LocalSize 64 1 1
%uint = OpTypeInt 32 0
%c16 = OpConstant %uint 16
%c2 = OpConstant %uint 2
%c0 = OpConstant %uint 0
%lds_arr = OpTypeArray %uint %c16
%_ptr_WG_lds = OpTypePointer Workgroup %lds_arr
%_ptr_WG_uint = OpTypePointer Workgroup %uint
%lds = OpVariable %_ptr_WG_lds Workgroup
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
%ptr = OpAccessChain %_ptr_WG_uint %lds %v_slot
%dummy = OpAtomicSMax %uint %ptr %c2 %c0 %v_data
OpReturn
OpFunctionEnd
"""
        interp = LdsAtomicMaxI32Interp(spvasm)
        seed_lds = {0: to_unsigned_u32(-1000), 1: to_unsigned_u32(-1000),
                    2: to_unsigned_u32(-1000), 3: to_unsigned_u32(-1000)}
        lane_inputs = []
        for i in range(64):
            slot = i % 4
            val = -500 + i * 15  # Spanning -500 up to +445
            lane_inputs.append({"%v_slot": slot, "%v_data": to_unsigned_u32(val)})

        final_lds, _ = interp.run(num_invocations=64, seed_lds=seed_lds, lane_inputs=lane_inputs)

        # Expected maximum for each slot s in {0, 1, 2, 3}: max over i with i % 4 == s
        for s in range(4):
            expected = max(-500 + i * 15 for i in range(64) if i % 4 == s)
            self.assertEqual(to_signed_i32(final_lds[s]), expected)


class TestTwosComplementSignedBoundaries(unittest.TestCase):
    """Verifies signed 32-bit extreme boundaries, identity elements, and sign-bit coverage."""

    def test_signed_32bit_extreme_boundaries(self):
        # INT32_MIN = -2147483648 (0x80000000)
        # INT32_MAX = 2147483647 (0x7FFFFFFF)
        # -1 = 0xFFFFFFFF
        # 0 = 0x00000000
        # +1 = 0x00000001
        cases = [
            (INT32_MIN, INT32_MAX, INT32_MAX),
            (INT32_MAX, INT32_MIN, INT32_MAX),
            (INT32_MIN, 0xFFFFFFFF, 0xFFFFFFFF),  # -2147483648 vs -1 -> -1
            (0xFFFFFFFF, INT32_MIN, 0xFFFFFFFF),
            (0xFFFFFFFF, 0, 0),                    # -1 vs 0 -> 0
            (0, 0xFFFFFFFF, 0),
            (0xFFFFFFFF, 1, 1),                    # -1 vs +1 -> +1
            (1, 0xFFFFFFFF, 1),
            (INT32_MIN, INT32_MIN + 1, INT32_MIN + 1),  # -2147483648 vs -2147483647 -> -2147483647
            (INT32_MAX - 1, INT32_MAX, INT32_MAX),      # 2147483646 vs 2147483647 -> 2147483647
        ]
        for a, b, expected in cases:
            with self.subTest(a=hex(a), b=hex(b)):
                self.assertEqual(reference_atomic_max_i32(a, b), expected)

    def test_int32_min_identity_element(self):
        # max(INT32_MIN, x) == x for all x
        test_values = [
            INT32_MIN,
            INT32_MIN + 1,
            -1000000,
            -65536,
            -1,
            0,
            1,
            65535,
            1000000,
            INT32_MAX - 1,
            INT32_MAX,
        ]
        for val in test_values:
            with self.subTest(val=val):
                u_val = to_unsigned_u32(val)
                self.assertEqual(reference_atomic_max_i32(INT32_MIN, u_val), u_val)
                self.assertEqual(reference_atomic_max_i32(u_val, INT32_MIN), u_val)

        # SPIR-V execution with INT32_MIN seed
        spvasm = """
OpCapability Shader
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main"
OpExecutionMode %main LocalSize 1 1 1
%uint = OpTypeInt 32 0
%c16 = OpConstant %uint 16
%c2 = OpConstant %uint 2
%c0 = OpConstant %uint 0
%c0_slot = OpConstant %uint 0
%lds_arr = OpTypeArray %uint %c16
%_ptr_WG_lds = OpTypePointer Workgroup %lds_arr
%_ptr_WG_uint = OpTypePointer Workgroup %uint
%lds = OpVariable %_ptr_WG_lds Workgroup
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
%ptr = OpAccessChain %_ptr_WG_uint %lds %c0_slot
%dummy = OpAtomicSMax %uint %ptr %c2 %c0 %v_data
OpReturn
OpFunctionEnd
"""
        interp = LdsAtomicMaxI32Interp(spvasm)
        for val in test_values:
            u_val = to_unsigned_u32(val)
            final_lds, _ = interp.run(seed_lds={0: INT32_MIN}, lane_inputs=[{"%v_data": u_val}])
            self.assertEqual(final_lds[0], u_val)

    def test_int32_max_absorbing_element(self):
        # max(INT32_MAX, x) == INT32_MAX for all x
        test_values = [
            INT32_MIN,
            INT32_MIN + 1,
            -500,
            -1,
            0,
            1,
            10000,
            INT32_MAX - 1,
            INT32_MAX,
        ]
        for val in test_values:
            with self.subTest(val=val):
                u_val = to_unsigned_u32(val)
                self.assertEqual(reference_atomic_max_i32(INT32_MAX, u_val), INT32_MAX)
                self.assertEqual(reference_atomic_max_i32(u_val, INT32_MAX), INT32_MAX)

        # SPIR-V execution with INT32_MAX seed
        spvasm = """
OpCapability Shader
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main"
OpExecutionMode %main LocalSize 1 1 1
%uint = OpTypeInt 32 0
%c16 = OpConstant %uint 16
%c2 = OpConstant %uint 2
%c0 = OpConstant %uint 0
%c0_slot = OpConstant %uint 0
%lds_arr = OpTypeArray %uint %c16
%_ptr_WG_lds = OpTypePointer Workgroup %lds_arr
%_ptr_WG_uint = OpTypePointer Workgroup %uint
%lds = OpVariable %_ptr_WG_lds Workgroup
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
%ptr = OpAccessChain %_ptr_WG_uint %lds %c0_slot
%dummy = OpAtomicSMax %uint %ptr %c2 %c0 %v_data
OpReturn
OpFunctionEnd
"""
        interp = LdsAtomicMaxI32Interp(spvasm)
        for val in test_values:
            u_val = to_unsigned_u32(val)
            final_lds, _ = interp.run(seed_lds={0: INT32_MAX}, lane_inputs=[{"%v_data": u_val}])
            self.assertEqual(final_lds[0], INT32_MAX)

    def test_sign_bit_matrix_coverage(self):
        # Exhaustive sign combinations: (+, +), (+, -), (-, +), (-, -)
        sign_matrix = [
            (10, 20, 20),           # (+, +)
            (20, 10, 20),
            (15, -30, 15),          # (+, -)
            (-30, 15, 15),          # (-, +)
            (-50, -20, -20),        # (-, -)
            (-20, -50, -20),
            (0x7FFFFFFF, 0x80000000, 0x7FFFFFFF),  # sign bit 0 vs 1
            (0x80000000, 0x7FFFFFFF, 0x7FFFFFFF),  # sign bit 1 vs 0
        ]
        for a, b, exp in sign_matrix:
            ua = to_unsigned_u32(a)
            ub = to_unsigned_u32(b)
            uexp = to_unsigned_u32(exp)
            self.assertEqual(reference_atomic_max_i32(ua, ub), uexp)


class TestReturnValueRetention(unittest.TestCase):
    """Verifies that returning forms preserve prior values and non-returning forms do not clobber."""

    def test_returning_form_preserves_unmodified_old_value(self):
        spvasm = """
OpCapability Shader
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main" %out
OpExecutionMode %main LocalSize 1 1 1
%uint = OpTypeInt 32 0
%_ptr_Output_uint = OpTypePointer Output %uint
%out = OpVariable %_ptr_Output_uint Output
%c16 = OpConstant %uint 16
%c2 = OpConstant %uint 2
%c0 = OpConstant %uint 0
%c0_slot = OpConstant %uint 0
%lds_arr = OpTypeArray %uint %c16
%_ptr_WG_lds = OpTypePointer Workgroup %lds_arr
%_ptr_WG_uint = OpTypePointer Workgroup %uint
%lds = OpVariable %_ptr_WG_lds Workgroup
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
%ptr = OpAccessChain %_ptr_WG_uint %lds %c0_slot
%old = OpAtomicSMax %uint %ptr %c2 %c0 %v_data
OpStore %out %old
OpReturn
OpFunctionEnd
"""
        interp = LdsAtomicMaxI32Interp(spvasm)
        # Test 1: Memory updates from negative to positive
        final_lds, lane_out = interp.run(seed_lds={0: to_unsigned_u32(-40)}, lane_inputs=[{"%v_data": 70}])
        self.assertEqual(lane_out[0]["%out"], to_unsigned_u32(-40))
        self.assertEqual(final_lds[0], 70)

        # Test 2: Memory stays same because input is smaller
        final_lds2, lane_out2 = interp.run(seed_lds={0: 70}, lane_inputs=[{"%v_data": to_unsigned_u32(-10)}])
        self.assertEqual(lane_out2[0]["%out"], 70)
        self.assertEqual(final_lds2[0], 70)

        # Test 3: Input is INT32_MIN, memory is INT32_MAX
        final_lds3, lane_out3 = interp.run(seed_lds={0: INT32_MAX}, lane_inputs=[{"%v_data": INT32_MIN}])
        self.assertEqual(lane_out3[0]["%out"], INT32_MAX)
        self.assertEqual(final_lds3[0], INT32_MAX)

    def test_multi_step_return_value_chain(self):
        # 5 sequential atomic operations chain through LDS[0]
        spvasm = """
OpCapability Shader
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main" %out1 %out2 %out3 %out4 %out5
OpExecutionMode %main LocalSize 1 1 1
%uint = OpTypeInt 32 0
%_ptr_Output_uint = OpTypePointer Output %uint
%out1 = OpVariable %_ptr_Output_uint Output
%out2 = OpVariable %_ptr_Output_uint Output
%out3 = OpVariable %_ptr_Output_uint Output
%out4 = OpVariable %_ptr_Output_uint Output
%out5 = OpVariable %_ptr_Output_uint Output
%c16 = OpConstant %uint 16
%c2 = OpConstant %uint 2
%c0 = OpConstant %uint 0
%c0_slot = OpConstant %uint 0
%c_minus50 = OpConstant %uint 4294967246
%c_minus20 = OpConstant %uint 4294967276
%c_minus30 = OpConstant %uint 4294967266
%c_plus10 = OpConstant %uint 10
%c_plus5 = OpConstant %uint 5
%lds_arr = OpTypeArray %uint %c16
%_ptr_WG_lds = OpTypePointer Workgroup %lds_arr
%_ptr_WG_uint = OpTypePointer Workgroup %uint
%lds = OpVariable %_ptr_WG_lds Workgroup
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
%ptr = OpAccessChain %_ptr_WG_uint %lds %c0_slot
%r1 = OpAtomicSMax %uint %ptr %c2 %c0 %c_minus50
OpStore %out1 %r1
%r2 = OpAtomicSMax %uint %ptr %c2 %c0 %c_minus20
OpStore %out2 %r2
%r3 = OpAtomicSMax %uint %ptr %c2 %c0 %c_minus30
OpStore %out3 %r3
%r4 = OpAtomicSMax %uint %ptr %c2 %c0 %c_plus10
OpStore %out4 %r4
%r5 = OpAtomicSMax %uint %ptr %c2 %c0 %c_plus5
OpStore %out5 %r5
OpReturn
OpFunctionEnd
"""
        interp = LdsAtomicMaxI32Interp(spvasm)
        # Seed LDS[0] with -100 (0xFFFFFF9C)
        seed_val = to_unsigned_u32(-100)
        final_lds, lane_out = interp.run(seed_lds={0: seed_val})

        # Op 1: max(-100, -50) -> returns -100, memory becomes -50
        self.assertEqual(to_signed_i32(lane_out[0]["%out1"]), -100)
        # Op 2: max(-50, -20) -> returns -50, memory becomes -20
        self.assertEqual(to_signed_i32(lane_out[0]["%out2"]), -50)
        # Op 3: max(-20, -30) -> returns -20, memory remains -20 (-20 > -30)
        self.assertEqual(to_signed_i32(lane_out[0]["%out3"]), -20)
        # Op 4: max(-20, +10) -> returns -20, memory becomes +10
        self.assertEqual(to_signed_i32(lane_out[0]["%out4"]), -20)
        # Op 5: max(+10, +5) -> returns +10, memory remains +10 (+10 > +5)
        self.assertEqual(to_signed_i32(lane_out[0]["%out5"]), 10)

        # Final memory state
        self.assertEqual(final_lds[0], 10)


class TestNegativeControlsAndErrors(unittest.TestCase):
    """Verifies that invalid constructs, unmodelled opcodes, and malformed headers fail."""

    def test_unsupported_storage_class_rejected(self):
        spvasm = """
OpCapability Shader
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main"
OpExecutionMode %main LocalSize 1 1 1
%uint = OpTypeInt 32 0
%_ptr_UC = OpTypePointer UniformConstant %uint
%u = OpVariable %_ptr_UC UniformConstant
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
OpReturn
OpFunctionEnd
"""
        with self.assertRaises(UnsupportedConstruct):
            LdsAtomicMaxI32Interp(spvasm)

    def test_unsupported_opcode_rejected(self):
        spvasm = """
OpCapability Shader
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main"
OpExecutionMode %main LocalSize 1 1 1
%uint = OpTypeInt 32 0
%c16 = OpConstant %uint 16
%lds_arr = OpTypeArray %uint %c16
%_ptr_WG_lds = OpTypePointer Workgroup %lds_arr
%lds = OpVariable %_ptr_WG_lds Workgroup
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
%bad = OpSDiv %uint %c16 %c16
OpReturn
OpFunctionEnd
"""
        interp = LdsAtomicMaxI32Interp(spvasm)
        with self.assertRaises(UnsupportedConstruct):
            interp.run()

    def test_invalid_workgroup_size_rejected(self):
        for dims in [(0, 1, 1), (1025, 1, 1), (32, 33, 1)]:
            with self.subTest(dims=dims):
                spvasm = f"""
OpCapability Shader
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main"
OpExecutionMode %main LocalSize {dims[0]} {dims[1]} {dims[2]}
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
OpReturn
OpFunctionEnd
"""
                with self.assertRaises(WorkgroupBoundsError):
                    LdsAtomicMaxI32Interp(spvasm)

    def test_atomic_on_non_lds_pointer_rejected(self):
        spvasm = """
OpCapability Shader
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main"
OpExecutionMode %main LocalSize 1 1 1
%uint = OpTypeInt 32 0
%_ptr_fn_uint = OpTypePointer Function %uint
%c2 = OpConstant %uint 2
%c0 = OpConstant %uint 0
%c5 = OpConstant %uint 5
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
%local_v = OpVariable %_ptr_fn_uint Function
%bad = OpAtomicSMax %uint %local_v %c2 %c0 %c5
OpReturn
OpFunctionEnd
"""
        interp = LdsAtomicMaxI32Interp(spvasm)
        with self.assertRaises(UnsupportedConstruct):
            interp.run()

    def test_lds_access_out_of_bounds_rejected(self):
        spvasm = """
OpCapability Shader
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main"
OpExecutionMode %main LocalSize 1 1 1
%uint = OpTypeInt 32 0
%c16 = OpConstant %uint 16
%c2 = OpConstant %uint 2
%c0 = OpConstant %uint 0
%c999 = OpConstant %uint 999
%c5 = OpConstant %uint 5
%lds_arr = OpTypeArray %uint %c16
%_ptr_WG_lds = OpTypePointer Workgroup %lds_arr
%_ptr_WG_uint = OpTypePointer Workgroup %uint
%lds = OpVariable %_ptr_WG_lds Workgroup
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
%ptr = OpAccessChain %_ptr_WG_uint %lds %c999
%bad = OpAtomicSMax %uint %ptr %c2 %c0 %c5
OpReturn
OpFunctionEnd
"""
        interp = LdsAtomicMaxI32Interp(spvasm)
        with self.assertRaises(WorkgroupBoundsError):
            interp.run()

    def test_multiple_workgroup_variables_rejected(self):
        spvasm = """
OpCapability Shader
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main"
OpExecutionMode %main LocalSize 1 1 1
%uint = OpTypeInt 32 0
%c16 = OpConstant %uint 16
%lds_arr = OpTypeArray %uint %c16
%_ptr_WG_lds = OpTypePointer Workgroup %lds_arr
%lds1 = OpVariable %_ptr_WG_lds Workgroup
%lds2 = OpVariable %_ptr_WG_lds Workgroup
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
OpReturn
OpFunctionEnd
"""
        with self.assertRaises(UnsupportedConstruct):
            LdsAtomicMaxI32Interp(spvasm)

    def test_non_array_workgroup_variable_rejected(self):
        spvasm = """
OpCapability Shader
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main"
OpExecutionMode %main LocalSize 1 1 1
%uint = OpTypeInt 32 0
%_ptr_WG_uint = OpTypePointer Workgroup %uint
%lds = OpVariable %_ptr_WG_uint Workgroup
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
OpReturn
OpFunctionEnd
"""
        with self.assertRaises(UnsupportedConstruct):
            LdsAtomicMaxI32Interp(spvasm)

    def test_non_32bit_int_rejected(self):
        spvasm = """
OpCapability Shader
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main"
OpExecutionMode %main LocalSize 1 1 1
%ulong = OpTypeInt 64 0
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
OpReturn
OpFunctionEnd
"""
        with self.assertRaises(UnsupportedConstruct):
            LdsAtomicMaxI32Interp(spvasm)

    def test_variable_storage_class_mismatch_rejected(self):
        spvasm = """
OpCapability Shader
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main"
OpExecutionMode %main LocalSize 1 1 1
%uint = OpTypeInt 32 0
%_ptr_WG_uint = OpTypePointer Workgroup %uint
%bad_var = OpVariable %_ptr_WG_uint Function
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
OpReturn
OpFunctionEnd
"""
        with self.assertRaises(UnsupportedConstruct):
            LdsAtomicMaxI32Interp(spvasm)

    def test_undefined_identifier_rejected(self):
        spvasm = """
OpCapability Shader
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main"
OpExecutionMode %main LocalSize 1 1 1
%uint = OpTypeInt 32 0
%c16 = OpConstant %uint 16
%c2 = OpConstant %uint 2
%c0 = OpConstant %uint 0
%c0_slot = OpConstant %uint 0
%lds_arr = OpTypeArray %uint %c16
%_ptr_WG_lds = OpTypePointer Workgroup %lds_arr
%_ptr_WG_uint = OpTypePointer Workgroup %uint
%lds = OpVariable %_ptr_WG_lds Workgroup
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
%ptr = OpAccessChain %_ptr_WG_uint %lds %c0_slot
%old = OpAtomicSMax %uint %ptr %c2 %c0 %undefined_val
OpReturn
OpFunctionEnd
"""
        interp = LdsAtomicMaxI32Interp(spvasm)
        with self.assertRaises(UnsupportedConstruct):
            interp.run()

    def test_unknown_branch_label_rejected(self):
        spvasm = """
OpCapability Shader
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main"
OpExecutionMode %main LocalSize 1 1 1
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
OpBranch %ghost_label
OpReturn
OpFunctionEnd
"""
        interp = LdsAtomicMaxI32Interp(spvasm)
        with self.assertRaises(UnsupportedConstruct):
            interp.run()


if __name__ == "__main__":
    unittest.main()
