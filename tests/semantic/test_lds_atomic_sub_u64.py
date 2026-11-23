"""Semantic interpreter tests for 64-bit LDS atomic subtract (ds_sub_u64, ds_sub_rtn_u64).

Standard library only; no GPU, no LLVM, no external tools, no game files.

    python -I tests/semantic/test_lds_atomic_sub_u64.py
    python -m unittest discover -s tests/semantic -v

Background & Specification Rules:
  * AMD RDNA2 / GFX10 Local Data Share (LDS) Architecture:
    - LDS is an on-chip, software-managed scratchpad memory shared across all threads
      in a workgroup (threadgroup).
    - Physical layout:
      Organized into 32 independent memory banks, each 4 bytes (32-bit dword) wide.
      Bank index for dword address `idx` is `idx % 32`.
      A 64-bit element occupies two consecutive dwords: low dword at `idx`, high dword at `idx + 1`.
      Consequently, a 64-bit LDS access simultaneously references bank `idx % 32` and `(idx + 1) % 32`.
    - `ds_sub_u64 vaddr, vdata[0:1] [offset:N]`:
        Atomically computes:
          effective_byte = (vaddr + byte_offset) & 0xFFFFFFFF
          dword_idx = (effective_byte >> 2) & (lds_dwords - 1)
          old_val = LDS[dword_idx] | (LDS[dword_idx + 1] << 32)
          new_val = (old_val - vdata) & 0xFFFFFFFFFFFFFFFF
          LDS[dword_idx] = new_val & 0xFFFFFFFF
          LDS[dword_idx + 1] = (new_val >> 32) & 0xFFFFFFFF
        Non-returning form: the prior value in LDS is discarded; destination registers are unmodified.
    - `ds_sub_rtn_u64 vdst[0:1], vaddr, vdata[0:1] [offset:N]`:
        Atomically computes:
          effective_byte = (vaddr + byte_offset) & 0xFFFFFFFF
          dword_idx = (effective_byte >> 2) & (lds_dwords - 1)
          old_val = LDS[dword_idx] | (LDS[dword_idx + 1] << 32)
          new_val = (old_val - vdata) & 0xFFFFFFFFFFFFFFFF
          LDS[dword_idx] = new_val & 0xFFFFFFFF
          LDS[dword_idx + 1] = (new_val >> 32) & 0xFFFFFFFF
          vdst[0] = old_val & 0xFFFFFFFF
          vdst[1] = (old_val >> 32) & 0xFFFFFFFF
        Returning form: writes the pre-subtraction 64-bit value into destination VGPR pair vdst[0:1].
    - Borrow propagation & 64-bit two's complement:
        Unlike bitwise operations, subtraction has inter-dword carry/borrow dependency.
        Borrow from low to high dword occurs when (old_val & 0xFFFFFFFF) < (vdata & 0xFFFFFFFF).
        Arithmetic is strictly modulo 2^64 without saturation or exception traps.
    - Active lane masking (EXEC):
        LDS instructions are vector operations executed across SIMD waves (Wave32 / Wave64).
        Only active lanes (where EXEC mask bit is 1) perform atomic operations; inactive lanes
        are no-ops and preserve destination registers.
    - Multi-lane serialization and address clashes:
        When multiple active lanes target the exact same 64-bit address, the hardware atomic unit
        serializes requests. All subtractions accumulate deterministically, and returning forms
        receive intermediate pre-subtraction values forming a consistent prefix chain.
    - Bank conflicts:
        When active lanes simultaneously target different addresses that map to identical banks,
        the hardware arbiter stalls and serializes the accesses into conflict cycles/phases.
    - Alignment and wrapping:
        64-bit element addresses are naturally 8-byte aligned (bits [2:0] = 0).
        Unaligned addresses either cross dword boundaries, bank boundaries, or cause hardware faults.
  * SPIR-V Representation:
    - LDS is allocated in the Workgroup storage class as an array of 64-bit integers (%ulong)
      or 32-bit integers (%uint).
    - 64-bit atomic subtract maps to `OpAtomicISub %ulong %ptr %scope %semantics %val`
      where %scope is Workgroup (2) and %semantics is Relaxed (0).
"""
from __future__ import annotations

import os
import random
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TOOLS_DIR = os.path.join(ROOT, "tools")
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

import rdna2spirv  # noqa: E402

MASK32 = 0xFFFFFFFF
MASK64 = 0xFFFFFFFFFFFFFFFF
SIGN_BIT_64 = 1 << 63
LDS_BANK_COUNT = 32
LDS_BANK_BYTES = 4


class UnsupportedConstruct(NotImplementedError):
    """The module uses an unmodelled SPIR-V opcode, type, or storage class."""


class WorkgroupBoundsError(ValueError):
    """Workgroup dimension or memory bounds violation."""


class AlignmentError(ValueError):
    """Address alignment violation for 64-bit access."""


_IGNORED = frozenset({
    "OpCapability", "OpExtInstImport", "OpMemoryModel", "OpEntryPoint", "OpExecutionMode",
    "OpDecorate", "OpMemberDecorate", "OpTypeVoid", "OpTypeFunction", "OpLabel",
    "OpReturn", "OpFunctionEnd", "OpName", "OpSource", "OpSelectionMerge",
})

_LINE = re.compile(r"^(?:(%\S+)\s*=\s*)?(Op\w+)\s*(.*)$")


def reference_atomic_sub_u64(initial: int, subtrahend: int) -> int:
    """Reference 64-bit unsigned wrapping atomic subtraction (modulo 2^64)."""
    return (initial - subtrahend) & MASK64


def reference_atomic_sub_u64_with_borrow(
    initial_lo: int, initial_hi: int, sub_lo: int, sub_hi: int
) -> tuple[int, int, int]:
    """Reference 64-bit subtraction split into 32-bit dwords with borrow propagation.

    Returns (result_lo, result_hi, borrow_out).
    """
    ini_lo = initial_lo & MASK32
    ini_hi = initial_hi & MASK32
    s_lo = sub_lo & MASK32
    s_hi = sub_hi & MASK32

    borrow = 1 if ini_lo < s_lo else 0
    res_lo = (ini_lo - s_lo) & MASK32
    res_hi = (ini_hi - s_hi - borrow) & MASK32
    borrow_out = 1 if (ini_hi < s_hi + borrow) else 0
    return res_lo, res_hi, borrow_out


def pack_u64(lo: int, hi: int) -> int:
    """Pack two 32-bit dwords into a 64-bit unsigned integer (little-endian)."""
    return ((hi & MASK32) << 32) | (lo & MASK32)


def unpack_u64(val: int) -> tuple[int, int]:
    """Unpack a 64-bit integer into low and high 32-bit dwords (little-endian)."""
    return val & MASK32, (val >> 32) & MASK32


def signed_i64(val: int) -> int:
    """Convert 64-bit unsigned integer to signed two's complement integer."""
    v = val & MASK64
    return v - (1 << 64) if v >= SIGN_BIT_64 else v


def unsigned_u64(val: int) -> int:
    """Mask to 64-bit unsigned integer."""
    return val & MASK64


def reference_lds_index_u64(base_addr: int, byte_offset: int, lds_dwords: int) -> tuple[int, int]:
    """Reference dword index calculation for 64-bit LDS access.

    Returns (dword_lo, dword_hi) taking hardware wrapping into account.
    """
    effective_byte = (base_addr + byte_offset) & MASK32
    dword_lo = (effective_byte >> 2) & (lds_dwords - 1)
    dword_hi = (dword_lo + 1) & (lds_dwords - 1)
    return dword_lo, dword_hi


class LdsAtomic64Interp:
    """Strict semantic interpreter for SPIR-V modules exercising 64-bit LDS atomics."""

    def __init__(self, spvasm: str):
        self.spvasm = spvasm
        self.types: dict[str, tuple] = {}
        self.constants: dict[str, int | bool] = {}
        self.ptr_types: dict[str, tuple[str, str]] = {}
        self.var_class: dict[str, str] = {}
        self.local_size = (1, 1, 1)
        self.lds_var: str | None = None
        self.lds_elem_type: str = "uint"
        self.lds_elements = 0
        self.body: list[tuple[str | None, str, list[str], str]] = []
        self._parse()

    def _parse(self):
        in_body = False
        for raw in self.spvasm.splitlines():
            line = raw.strip()
            if not line or line.startswith(";"):
                continue
            m = _LINE.match(line)
            if not m:
                raise UnsupportedConstruct(f"Cannot parse line: {line!r}")
            res, op, rest = m.groups()
            a = rest.split()
            if in_body:
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
                width = int(a[0])
                if width not in (32, 64):
                    raise UnsupportedConstruct(f"Only 32-bit and 64-bit int supported, got {width}")
                signedness = int(a[1]) if len(a) > 1 else 0
                self.types[res] = ("int", width, signedness)
            elif op == "OpTypeBool":
                self.types[res] = ("bool",)
            elif op == "OpTypeVector":
                self.types[res] = ("vec", a[0], int(a[1]))
            elif op == "OpTypeArray":
                size = int(self.constants[a[1]])
                elem_type = a[0]
                self.types[res] = ("arr", elem_type, size)
            elif op == "OpTypePointer":
                self.ptr_types[res] = (a[0], a[1])
            elif op == "OpConstant":
                ty = self.types.get(a[0])
                val_raw = int(a[1], 0)
                if ty and ty[0] == "int" and ty[1] == 64:
                    self.constants[res] = val_raw & MASK64
                else:
                    self.constants[res] = val_raw & MASK32
            elif op == "OpConstantComposite":
                pass
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
                    self.lds_elements = arr_ty[2]
                    elem_type_info = self.types[arr_ty[1]]
                    self.lds_elem_type = "ulong" if elem_type_info[1] == 64 else "uint"
                elif cls not in ("Output", "Function"):
                    raise UnsupportedConstruct(f"Unsupported storage class {cls}")
            else:
                raise UnsupportedConstruct(f"Header opcode {op}")

    def run(self, num_invocations: int | None = None, seed_lds: dict[int, int] | None = None,
            lane_inputs: list[dict[str, int]] | None = None) -> tuple[list[int], list[dict[str, int]]]:
        """Executes the workgroup across invocations and returns (final_lds, outputs_per_lane)."""
        lx, ly, lz = self.local_size
        total_invocations = lx * ly * lz
        count = total_invocations if num_invocations is None else min(num_invocations, total_invocations)

        elem_mask = MASK64 if self.lds_elem_type == "ulong" else MASK32
        lds = [0] * self.lds_elements
        if seed_lds:
            for idx, val in seed_lds.items():
                if 0 <= idx < self.lds_elements:
                    lds[idx] = val & elem_mask

        lane_outputs: list[dict[str, int]] = []
        for lidx in range(count):
            inputs = lane_inputs[lidx] if lane_inputs and lidx < len(lane_inputs) else {}
            out = self._run_lane(lidx, lds, inputs)
            lane_outputs.append(out)

        return list(lds), lane_outputs

    def _run_lane(self, lidx: int, lds: list[int], inputs: dict[str, int]) -> dict[str, int]:
        values: dict[str, int | bool] = dict(self.constants)
        values.update(inputs)
        var_box: dict[str, list[int]] = {}
        pointers: dict[str, tuple] = {}
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

        def val(x: str) -> int:
            if x in values:
                return int(values[x])
            raise UnsupportedConstruct(f"Undefined identifier {x}")

        for res, op, a, _ in self.body:
            if op in _IGNORED or op in ("OpBranch", "OpBranchConditional"):
                continue
            if op == "OpVariable":
                cls, _ = self.ptr_types[a[0]]
                if cls != "Function":
                    raise UnsupportedConstruct("Non-Function variable in body")
                var_box[res] = [val(a[2]) if len(a) > 2 else 0]
                pointers[res] = ("fn", var_box[res], 0)
            elif op == "OpAccessChain":
                base = a[1]
                if base not in pointers:
                    raise UnsupportedConstruct(f"OpAccessChain on non-pointer {base}")
                ptag = pointers[base][0]
                if ptag == "lds":
                    idx = val(a[2])
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
                mask = MASK64 if self.lds_elem_type == "ulong" else MASK32
                if ptag == "lds":
                    values[res] = ptr[1][ptr[2]] & mask
                elif ptag in ("out", "fn"):
                    values[res] = ptr[1][ptr[2]]
            elif op == "OpStore":
                ptr = pointers[a[0]]
                ptag = ptr[0]
                mask = MASK64 if self.lds_elem_type == "ulong" else MASK32
                v = val(a[1]) & mask
                if ptag == "lds":
                    ptr[1][ptr[2]] = v
                elif ptag in ("out", "fn"):
                    ptr[1][ptr[2]] = v
            elif op == "OpAtomicISub":
                # %res = OpAtomicISub %type %ptr %scope %semantics %val
                ty_name = a[0]
                ty_info = self.types[ty_name]
                is_64 = (ty_info[1] == 64)
                mask = MASK64 if is_64 else MASK32

                ptr = pointers[a[1]]
                ptag = ptr[0]
                if ptag != "lds":
                    raise UnsupportedConstruct(f"OpAtomicISub on non-LDS pointer {ptag}")
                subtrahend = val(a[4]) & mask
                old = ptr[1][ptr[2]] & mask
                ptr[1][ptr[2]] = (old - subtrahend) & mask
                values[res] = old
            elif op == "OpIAdd":
                values[res] = (val(a[1]) + val(a[2])) & MASK64
            elif op == "OpISub":
                values[res] = (val(a[1]) - val(a[2])) & MASK64
            elif op == "OpShiftRightLogical":
                sh = val(a[2])
                values[res] = (val(a[1]) >> sh) & MASK64
            elif op == "OpShiftLeftLogical":
                sh = val(a[2])
                values[res] = (val(a[1]) << sh) & MASK64
            elif op == "OpBitwiseAnd":
                values[res] = val(a[1]) & val(a[2]) & MASK64
            elif op == "OpBitwiseOr":
                values[res] = (val(a[1]) | val(a[2])) & MASK64
            elif op == "OpBitwiseXor":
                values[res] = (val(a[1]) ^ val(a[2])) & MASK64
            elif op == "OpSelect":
                cond = bool(values[a[1]])
                values[res] = val(a[2]) if cond else val(a[3])
            elif op == "OpCopyObject":
                values[res] = val(a[1])
            elif op == "OpBitcast":
                values[res] = val(a[1])
            else:
                raise UnsupportedConstruct(f"Opcode {op} not modelled in body")

        return {name: var_box[name][0] for name in outputs}


class TestAtomicSubU64ReferenceMath(unittest.TestCase):
    """Verifies 64-bit unsigned wrapping arithmetic, borrow propagation, and edge cases."""

    def test_basic_subtraction(self):
        self.assertEqual(reference_atomic_sub_u64(100, 30), 70)
        self.assertEqual(reference_atomic_sub_u64(0, 0), 0)
        self.assertEqual(reference_atomic_sub_u64(100, 0), 100)
        self.assertEqual(reference_atomic_sub_u64(0x12345678_9ABCDEF0, 0), 0x12345678_9ABCDEF0)
        self.assertEqual(reference_atomic_sub_u64(0x12345678_9ABCDEF0, 0x12345678_9ABCDEF0), 0)

    def test_underflow_modulo_2_pow_64(self):
        cases = [
            (0, 1, MASK64),
            (0, 2, MASK64 - 1),
            (0, 5, 0xFFFFFFFFFFFFFFFB),
            (10, 20, MASK64 - 9),
            (0x80000000_00000000, 1, 0x7FFFFFFF_FFFFFFFF),
            (0, 0x80000000_00000000, 0x80000000_00000000),
            (MASK64, MASK64, 0),
            (0, MASK64, 1),
        ]
        for init, sub, expected in cases:
            with self.subTest(init=hex(init), sub=hex(sub)):
                self.assertEqual(reference_atomic_sub_u64(init, sub), expected)

    def test_dword_borrow_propagation(self):
        # When low dword has insufficient magnitude, borrow must decrement high dword
        initial = 0x00000005_00000000
        subtrahend = 0x00000000_00000001
        expected = 0x00000004_FFFFFFFF
        self.assertEqual(reference_atomic_sub_u64(initial, subtrahend), expected)

        ini_lo, ini_hi = unpack_u64(initial)
        sub_lo, sub_hi = unpack_u64(subtrahend)
        res_lo, res_hi, borrow = reference_atomic_sub_u64_with_borrow(ini_lo, ini_hi, sub_lo, sub_hi)
        self.assertEqual(borrow, 0)  # Borrow from lo to hi, but overall 64-bit operation doesn't borrow out
        self.assertEqual(pack_u64(res_lo, res_hi), expected)

    def test_dword_borrow_out_on_underflow(self):
        # When high dword also underflows, borrow_out must be 1
        initial = 0x00000000_00000000
        subtrahend = 0x00000000_00000001
        ini_lo, ini_hi = unpack_u64(initial)
        sub_lo, sub_hi = unpack_u64(subtrahend)
        res_lo, res_hi, borrow_out = reference_atomic_sub_u64_with_borrow(ini_lo, ini_hi, sub_lo, sub_hi)
        self.assertEqual(borrow_out, 1)
        self.assertEqual(pack_u64(res_lo, res_hi), MASK64)

    def test_twos_complement_addition_equivalence(self):
        # Subtracting X is identical to adding (-X) modulo 2^64
        rng = random.Random(0x50B64)
        for _ in range(50):
            a = rng.getrandbits(64)
            b = rng.getrandbits(64)
            neg_b = (-b) & MASK64
            sub_res = reference_atomic_sub_u64(a, b)
            add_res = (a + neg_b) & MASK64
            self.assertEqual(sub_res, add_res)

    def test_accumulative_subtraction_series(self):
        # Subtracting 1..100 from 5050 yields 0
        total = 5050
        for i in range(1, 101):
            total = reference_atomic_sub_u64(total, i)
        self.assertEqual(total, 0)

        # Subtracting 1..100 from 0 yields -5050 modulo 2^64
        total2 = 0
        for i in range(1, 101):
            total2 = reference_atomic_sub_u64(total2, i)
        self.assertEqual(total2, (-5050) & MASK64)

    def test_pseudorandom_fuzz_against_python_bigint(self):
        rng = random.Random(0xDECAFBAD)
        for _ in range(250):
            a = rng.getrandbits(64)
            b = rng.getrandbits(64)
            expected = (a - b) % (1 << 64)
            self.assertEqual(reference_atomic_sub_u64(a, b), expected)


class TestLdsAddressingAndOffsets64(unittest.TestCase):
    """Verifies byte offset math, 64-bit dword pair calculation, and hardware modulo wrapping."""

    def test_aligned_dword_pair_addressing(self):
        lds_dwords = 256
        for elem_idx in (0, 1, 2, 16, 64, 127):
            byte_addr = elem_idx * 8  # 8 bytes per 64-bit element
            dw_lo, dw_hi = reference_lds_index_u64(byte_addr, 0, lds_dwords)
            self.assertEqual(dw_lo, elem_idx * 2)
            self.assertEqual(dw_hi, elem_idx * 2 + 1)

    def test_static_offset_addition(self):
        lds_dwords = 512
        base = 64  # dword 16
        offset = 32  # +8 dwords
        dw_lo, dw_hi = reference_lds_index_u64(base, offset, lds_dwords)
        self.assertEqual(dw_lo, 24)
        self.assertEqual(dw_hi, 25)

    def test_boundary_wrapping_at_lds_end(self):
        lds_dwords = 64  # 256 bytes
        # Byte address 248 -> dwords (62, 63)
        self.assertEqual(reference_lds_index_u64(248, 0, lds_dwords), (62, 63))
        # Byte address 252 -> dwords (63, 0) [wrapping across boundary]
        self.assertEqual(reference_lds_index_u64(252, 0, lds_dwords), (63, 0))
        # Byte address 256 -> wraps completely to dword 0
        self.assertEqual(reference_lds_index_u64(256, 0, lds_dwords), (0, 1))


class TestSpirvAtomicSubU64Interpreter(unittest.TestCase):
    """Executes SPIR-V compute kernels with 64-bit LDS OpAtomicISub operations."""

    def test_ds_sub_u64_non_returning_semantic(self):
        # ds_sub_u64: updates LDS[target], returns nothing to registers
        spvasm = """
OpCapability Shader
OpCapability Int64
OpCapability Int64Atomics
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main"
OpExecutionMode %main LocalSize 1 1 1
%uint = OpTypeInt 32 0
%ulong = OpTypeInt 64 0
%c32 = OpConstant %uint 32
%c2 = OpConstant %uint 2
%c0 = OpConstant %uint 0
%c2_slot = OpConstant %uint 2
%c25 = OpConstant %ulong 25
%lds_arr = OpTypeArray %ulong %c32
%_ptr_WG_lds = OpTypePointer Workgroup %lds_arr
%_ptr_WG_ulong = OpTypePointer Workgroup %ulong
%lds = OpVariable %_ptr_WG_lds Workgroup
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
%ptr = OpAccessChain %_ptr_WG_ulong %lds %c2_slot
%dummy = OpAtomicISub %ulong %ptr %c2 %c0 %c25
OpReturn
OpFunctionEnd
"""
        interp = LdsAtomic64Interp(spvasm)
        final_lds, _ = interp.run(seed_lds={2: 100})
        # Initial 100 - 25 = 75
        self.assertEqual(final_lds[2], 75)
        # Other slots stay 0
        self.assertEqual(final_lds[0], 0)
        self.assertEqual(final_lds[1], 0)
        self.assertEqual(final_lds[3], 0)

    def test_ds_sub_rtn_u64_returning_semantic(self):
        # ds_sub_rtn_u64: updates LDS[target] AND returns prior 64-bit value
        spvasm = """
OpCapability Shader
OpCapability Int64
OpCapability Int64Atomics
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main" %out
OpExecutionMode %main LocalSize 1 1 1
%uint = OpTypeInt 32 0
%ulong = OpTypeInt 64 0
%_ptr_Output_ulong = OpTypePointer Output %ulong
%out = OpVariable %_ptr_Output_ulong Output
%c32 = OpConstant %uint 32
%c2 = OpConstant %uint 2
%c0 = OpConstant %uint 0
%c5_slot = OpConstant %uint 5
%c50 = OpConstant %ulong 50
%lds_arr = OpTypeArray %ulong %c32
%_ptr_WG_lds = OpTypePointer Workgroup %lds_arr
%_ptr_WG_ulong = OpTypePointer Workgroup %ulong
%lds = OpVariable %_ptr_WG_lds Workgroup
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
%ptr = OpAccessChain %_ptr_WG_ulong %lds %c5_slot
%old = OpAtomicISub %ulong %ptr %c2 %c0 %c50
OpStore %out %old
OpReturn
OpFunctionEnd
"""
        interp = LdsAtomic64Interp(spvasm)
        initial_val = 0x12345678_9ABCDEF0
        final_lds, lane_out = interp.run(seed_lds={5: initial_val})
        # Return value must be the OLD value
        self.assertEqual(lane_out[0]["%out"], initial_val)
        # Memory must contain the difference
        self.assertEqual(final_lds[5], initial_val - 50)

    def test_atomic_sub_u64_underflow_wrap(self):
        # Subtracting 1 from 0 yields 0xFFFFFFFFFFFFFFFF
        spvasm = """
OpCapability Shader
OpCapability Int64
OpCapability Int64Atomics
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main" %out
OpExecutionMode %main LocalSize 1 1 1
%uint = OpTypeInt 32 0
%ulong = OpTypeInt 64 0
%_ptr_Output_ulong = OpTypePointer Output %ulong
%out = OpVariable %_ptr_Output_ulong Output
%c16 = OpConstant %uint 16
%c2 = OpConstant %uint 2
%c0 = OpConstant %uint 0
%c0_slot = OpConstant %uint 0
%c1 = OpConstant %ulong 1
%lds_arr = OpTypeArray %ulong %c16
%_ptr_WG_lds = OpTypePointer Workgroup %lds_arr
%_ptr_WG_ulong = OpTypePointer Workgroup %ulong
%lds = OpVariable %_ptr_WG_lds Workgroup
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
%ptr = OpAccessChain %_ptr_WG_ulong %lds %c0_slot
%old = OpAtomicISub %ulong %ptr %c2 %c0 %c1
OpStore %out %old
OpReturn
OpFunctionEnd
"""
        interp = LdsAtomic64Interp(spvasm)
        final_lds, lane_out = interp.run(seed_lds={0: 0})
        self.assertEqual(lane_out[0]["%out"], 0)
        self.assertEqual(final_lds[0], MASK64)

    def test_chained_atomic_subtractions_same_slot(self):
        spvasm = """
OpCapability Shader
OpCapability Int64
OpCapability Int64Atomics
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main" %out
OpExecutionMode %main LocalSize 1 1 1
%uint = OpTypeInt 32 0
%ulong = OpTypeInt 64 0
%_ptr_Output_ulong = OpTypePointer Output %ulong
%out = OpVariable %_ptr_Output_ulong Output
%c16 = OpConstant %uint 16
%c2 = OpConstant %uint 2
%c0 = OpConstant %uint 0
%c1_slot = OpConstant %uint 1
%c100 = OpConstant %ulong 100
%c200 = OpConstant %ulong 200
%c300 = OpConstant %ulong 300
%lds_arr = OpTypeArray %ulong %c16
%_ptr_WG_lds = OpTypePointer Workgroup %lds_arr
%_ptr_WG_ulong = OpTypePointer Workgroup %ulong
%lds = OpVariable %_ptr_WG_lds Workgroup
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
%ptr = OpAccessChain %_ptr_WG_ulong %lds %c1_slot
%r1 = OpAtomicISub %ulong %ptr %c2 %c0 %c100
%r2 = OpAtomicISub %ulong %ptr %c2 %c0 %c200
%r3 = OpAtomicISub %ulong %ptr %c2 %c0 %c300
OpStore %out %r3
OpReturn
OpFunctionEnd
"""
        interp = LdsAtomic64Interp(spvasm)
        final_lds, lane_out = interp.run(seed_lds={1: 1000})
        # r1 got 1000 (lds became 900)
        # r2 got 900 (lds became 700)
        # r3 got 700 (lds became 400)
        self.assertEqual(lane_out[0]["%out"], 700)
        self.assertEqual(final_lds[1], 400)


if __name__ == "__main__":
    unittest.main()
