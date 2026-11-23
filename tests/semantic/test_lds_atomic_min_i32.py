"""Semantic interpreter tests for signed 32-bit LDS atomic minimum (ds_min_i32).

Standard library only; no GPU, no LLVM, no external tools, no game files.

    python -I tests/semantic/test_lds_atomic_min_i32.py
    python -m unittest discover -s tests/semantic -v

Background & Specification Rules:
  * AMD RDNA2 / GFX10 Local Data Share (LDS) Architecture:
    - LDS is an on-chip, software-managed scratchpad memory shared across all threads
      in a workgroup (threadgroup).
    - `ds_min_i32 vaddr, vdata [offset:N]`:
        Atomically computes:
          effective_byte = (vaddr + byte_offset) & 0xFFFFFFFF
          dword_idx = (effective_byte >> 2) & (lds_dwords - 1)
          LDS[dword_idx] = min_i32(LDS[dword_idx], vdata)
        Non-returning form: the prior value in LDS[dword_idx] is discarded.
    - `ds_min_rtn_i32 vdst, vaddr, vdata [offset:N]`:
        Atomically computes:
          effective_byte = (vaddr + byte_offset) & 0xFFFFFFFF
          dword_idx = (effective_byte >> 2) & (lds_dwords - 1)
          old_val = LDS[dword_idx]
          LDS[dword_idx] = min_i32(old_val, vdata)
          vdst = old_val
        Returning form: writes the pre-atomic value into destination VGPR vdst.
    - Two's-Complement Signed 32-bit Integer Semantics:
        Operands are interpreted as signed integers in [-2^31, 2^31 - 1].
        Negative values (bit 31 set) compare strictly less than non-negative values:
          e.g. -1 (0xFFFFFFFF) < 0 (0x00000000)
          e.g. INT32_MIN (-2^31, 0x80000000) < INT32_MAX (2^31 - 1, 0x7FFFFFFF)
        This is in sharp contrast to unsigned minimum `ds_min_u32` (OpAtomicUMin) where
        0x00000000 < 0xFFFFFFFF.
    - Active lane masking:
        LDS operations execute under execution mask control (%exec_active).
        Only active lanes participate in memory modifications and destination register writes.
    - Hardware wrapping & alignment:
        Byte addresses are converted to dwords by right shifting by 2, dropping unaligned
        low bits [1:0]. The dword index is masked with (lds_dwords - 1), wrapping out-of-bounds
        offsets within the allocated power-of-two LDS allocation.
    - 32-Bank Architecture & Address Clashes:
        LDS is organized into 32 banks (1 dword wide each, bank = (dword_idx) & 31).
        When multiple lanes access different dwords on the same bank, a bank conflict occurs
        and accesses are serialized across cycles.
        When multiple lanes access the exact same dword (address clash), the atomic operations
        are serialized in hardware. Because min_i32 is commutative and associative,
        arbitrary lane arbitration orders strictly converge to the identical global minimum.
  * SPIR-V Representation:
    - LDS is declared as an array in the Workgroup storage class:
        %lds_arr = OpTypeArray %uint <lds_dwords>
        %_ptr_WG_lds = OpTypePointer Workgroup %lds_arr
        %_ptr_WG_uint = OpTypePointer Workgroup %uint
        %lds = OpVariable %_ptr_WG_lds Workgroup
    - Atomic signed minimum maps directly to SPIR-V OpAtomicSMin:
        %old = OpAtomicSMin %uint %ptr %scope %semantics %val
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
UINT32_INT32_MIN = 0x80000000
UINT32_INT32_MAX = 0x7FFFFFFF


class UnsupportedConstruct(NotImplementedError):
    """The module uses an unmodelled SPIR-V opcode, type, or storage class."""


class WorkgroupBoundsError(ValueError):
    """Workgroup dimension or memory bounds violation."""


_IGNORED = frozenset({
    "OpCapability", "OpExtInstImport", "OpMemoryModel", "OpEntryPoint", "OpExecutionMode",
    "OpDecorate", "OpMemberDecorate", "OpTypeVoid", "OpTypeFunction", "OpLabel",
    "OpReturn", "OpFunctionEnd", "OpName", "OpSource", "OpSelectionMerge",
})

_LINE = re.compile(r"^(?:(%\S+)\s*=\s*)?(Op\w+)\s*(.*)$")


def to_signed_i32(val: int) -> int:
    """Interprets a 32-bit unsigned bit pattern as a signed two's-complement integer."""
    u = val & MASK
    return u - 0x100000000 if u & SIGN_BIT else u


def to_unsigned_u32(val: int) -> int:
    """Converts a signed or unsigned integer to a 32-bit unsigned bit pattern."""
    return val & MASK


def reference_atomic_smin(initial: int, val: int) -> int:
    """Reference 32-bit signed atomic minimum returning 32-bit bit pattern."""
    s_init = to_signed_i32(initial)
    s_val = to_signed_i32(val)
    return to_unsigned_u32(min(s_init, s_val))


def reference_atomic_umin(initial: int, val: int) -> int:
    """Reference 32-bit unsigned atomic minimum returning 32-bit bit pattern."""
    u_init = initial & MASK
    u_val = val & MASK
    return min(u_init, u_val)


def reference_lds_index(base_addr: int, byte_offset: int, lds_dwords: int) -> int:
    """Reference dword index calculation with hardware wrapping."""
    effective_byte = (base_addr + byte_offset) & MASK
    dword_idx = effective_byte >> 2
    return dword_idx & (lds_dwords - 1)


def reference_lds_bank(byte_addr: int) -> int:
    """Reference LDS bank index (32 banks, 4 bytes each, bank = (byte_addr >> 2) & 31)."""
    return (byte_addr >> 2) & 31


class LdsAtomicMinI32Interp:
    """Strict semantic interpreter for compute SPIR-V modules exercising LDS ds_min_i32 atomics."""

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
            line = raw.split(";", 1)[0].strip()
            if not line:
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

        for idx, (res, op, a, line) in enumerate(self.body):
            if op == "OpLabel" and res:
                self.label_indices[res] = idx

    def run(
        self,
        num_invocations: int | None = None,
        seed_lds: dict[int, int] | None = None,
        lane_inputs: list[dict[str, int | bool]] | None = None,
        lane_order: list[int] | None = None,
        exec_mask: int | None = None,
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
        lane_outputs: list[dict[str, int]] = [{} for _ in range(count)]

        for lidx in order:
            if lidx >= count:
                continue
            inputs = dict(lane_inputs[lidx]) if lane_inputs and lidx < len(lane_inputs) else {}
            if exec_mask is not None:
                inputs["%exec_active"] = bool((exec_mask >> lidx) & 1)
            out = self._run_lane(lidx, lds, inputs)
            lane_outputs[lidx] = out

        return list(lds), lane_outputs

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
                idx = ptr[2]
                assert idx is not None
                if ptag in ("lds", "out", "fn"):
                    values[res] = ptr[1][idx] & MASK
                else:
                    raise UnsupportedConstruct(f"OpLoad from {ptag}")
            elif op == "OpStore":
                ptr = pointers[a[0]]
                v = int(val(a[1])) & MASK
                ptag = ptr[0]
                idx = ptr[2]
                assert idx is not None
                if ptag in ("lds", "out", "fn"):
                    ptr[1][idx] = v
                else:
                    raise UnsupportedConstruct(f"OpStore to {ptag}")
            elif op == "OpAtomicSMin":
                # %res = OpAtomicSMin %type %ptr %scope %semantics %val
                ptr = pointers[a[1]]
                ptag = ptr[0]
                if ptag != "lds":
                    raise UnsupportedConstruct(f"OpAtomicSMin on non-LDS pointer {ptag}")
                operand = int(val(a[4])) & MASK
                idx = ptr[2]
                assert idx is not None
                old = ptr[1][idx] & MASK
                s_old = to_signed_i32(old)
                s_op = to_signed_i32(operand)
                ptr[1][idx] = to_unsigned_u32(min(s_old, s_op))
                values[res] = old
            elif op == "OpAtomicUMin":
                # %res = OpAtomicUMin %type %ptr %scope %semantics %val
                ptr = pointers[a[1]]
                ptag = ptr[0]
                if ptag != "lds":
                    raise UnsupportedConstruct(f"OpAtomicUMin on non-LDS pointer {ptag}")
                operand = int(val(a[4])) & MASK
                idx = ptr[2]
                assert idx is not None
                old = ptr[1][idx] & MASK
                ptr[1][idx] = min(old, operand)
                values[res] = old
            elif op == "OpIAdd":
                values[res] = (int(val(a[1])) + int(val(a[2]))) & MASK
            elif op == "OpISub":
                values[res] = (int(val(a[1])) - int(val(a[2]))) & MASK
            elif op == "OpShiftRightLogical":
                sh = int(val(a[2]))
                if sh >= 32:
                    raise UnsupportedConstruct(f"Shift right >= 32: {sh}")
                values[res] = (int(val(a[1])) >> sh) & MASK
            elif op == "OpShiftLeftLogical":
                sh = int(val(a[2]))
                if sh >= 32:
                    raise UnsupportedConstruct(f"Shift left >= 32: {sh}")
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


class TestSignedAtomicMinReferenceMath(unittest.TestCase):
    """Verifies reference math oracle for two's complement signed 32-bit minimum."""

    def test_basic_positive_values(self):
        self.assertEqual(reference_atomic_smin(100, 25), 25)
        self.assertEqual(reference_atomic_smin(25, 100), 25)
        self.assertEqual(reference_atomic_smin(7, 42), 7)
        self.assertEqual(reference_atomic_smin(0, 5), 0)
        self.assertEqual(reference_atomic_smin(0, 0), 0)

    def test_positive_vs_negative(self):
        # In signed comparison, any negative is less than any positive or zero
        self.assertEqual(to_signed_i32(reference_atomic_smin(10, to_unsigned_u32(-5))), -5)
        self.assertEqual(to_signed_i32(reference_atomic_smin(to_unsigned_u32(-5), 10)), -5)
        self.assertEqual(to_signed_i32(reference_atomic_smin(0, to_unsigned_u32(-1))), -1)
        self.assertEqual(to_signed_i32(reference_atomic_smin(to_unsigned_u32(-1), 0)), -1)
        self.assertEqual(to_signed_i32(reference_atomic_smin(500, to_unsigned_u32(-1000))), -1000)

    def test_negative_vs_negative(self):
        # More negative is strictly smaller
        self.assertEqual(to_signed_i32(reference_atomic_smin(to_unsigned_u32(-10), to_unsigned_u32(-50))), -50)
        self.assertEqual(to_signed_i32(reference_atomic_smin(to_unsigned_u32(-50), to_unsigned_u32(-10))), -50)
        self.assertEqual(to_signed_i32(reference_atomic_smin(to_unsigned_u32(-2), to_unsigned_u32(-1))), -2)
        self.assertEqual(to_signed_i32(reference_atomic_smin(to_unsigned_u32(-100), to_unsigned_u32(-100))), -100)

    def test_signed_vs_unsigned_divergence(self):
        # 0xFFFFFFFF is -1 in signed i32, but 4294967295 in unsigned u32
        # Comparison with +1:
        #   smin(+1, -1) -> -1 (0xFFFFFFFF)
        #   umin(+1, 4294967295) -> +1 (0x00000001)
        s_res = reference_atomic_smin(1, 0xFFFFFFFF)
        u_res = reference_atomic_umin(1, 0xFFFFFFFF)
        self.assertEqual(s_res, 0xFFFFFFFF)
        self.assertEqual(u_res, 1)
        self.assertNotEqual(s_res, u_res)

        # Comparison of 0 vs -2 (0xFFFFFFFE):
        #   smin(0, -2) -> -2 (0xFFFFFFFE)
        #   umin(0, 4294967294) -> 0 (0x00000000)
        s_res2 = reference_atomic_smin(0, 0xFFFFFFFE)
        u_res2 = reference_atomic_umin(0, 0xFFFFFFFE)
        self.assertEqual(s_res2, 0xFFFFFFFE)
        self.assertEqual(u_res2, 0)
        self.assertNotEqual(s_res2, u_res2)

        # Comparison of INT32_MAX (0x7FFFFFFF) vs INT32_MIN (0x80000000):
        #   smin(0x7FFFFFFF, 0x80000000) -> 0x80000000 (-2147483648)
        #   umin(0x7FFFFFFF, 0x80000000) -> 0x7FFFFFFF (2147483647)
        s_extremes = reference_atomic_smin(0x7FFFFFFF, 0x80000000)
        u_extremes = reference_atomic_umin(0x7FFFFFFF, 0x80000000)
        self.assertEqual(s_extremes, 0x80000000)
        self.assertEqual(u_extremes, 0x7FFFFFFF)
        self.assertNotEqual(s_extremes, u_extremes)

    def test_algebraic_properties(self):
        samples = [
            0, 1, -1, 42, -42, 1000, -1000,
            INT32_MIN_SIGNED, INT32_MAX_SIGNED,
            INT32_MIN_SIGNED + 1, INT32_MAX_SIGNED - 1
        ]
        u_samples = [to_unsigned_u32(x) for x in samples]

        # Idempotence: min(x, x) == x
        for x in u_samples:
            self.assertEqual(reference_atomic_smin(x, x), x)

        # Commutativity: min(a, b) == min(b, a)
        for a in u_samples:
            for b in u_samples:
                self.assertEqual(reference_atomic_smin(a, b), reference_atomic_smin(b, a))

        # Identity element (UINT32_INT32_MAX): min(x, INT32_MAX) == x
        for x in u_samples:
            self.assertEqual(reference_atomic_smin(x, UINT32_INT32_MAX), x)

        # Annihilator element (UINT32_INT32_MIN): min(x, INT32_MIN) == INT32_MIN
        for x in u_samples:
            self.assertEqual(reference_atomic_smin(x, UINT32_INT32_MIN), UINT32_INT32_MIN)


class TestSpirvAtomicSMinNonReturning(unittest.TestCase):
    """Executes compute SPIR-V kernels with LDS OpAtomicSMin non-returning operations."""

    def test_ds_min_i32_positive_smaller(self):
        # Initial 50, lane provides 20 -> final LDS is 20
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
%c20 = OpConstant %uint 20
%lds_arr = OpTypeArray %uint %c64
%_ptr_WG_lds = OpTypePointer Workgroup %lds_arr
%_ptr_WG_uint = OpTypePointer Workgroup %uint
%lds = OpVariable %_ptr_WG_lds Workgroup
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
%ptr = OpAccessChain %_ptr_WG_uint %lds %c3
%dummy = OpAtomicSMin %uint %ptr %c2 %c0 %c20
OpReturn
OpFunctionEnd
"""
        interp = LdsAtomicMinI32Interp(spvasm)
        final_lds, _ = interp.run(seed_lds={3: 50})
        self.assertEqual(final_lds[3], 20)

    def test_ds_min_i32_positive_larger(self):
        # Initial 20, lane provides 50 -> final LDS remains 20
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
%c50 = OpConstant %uint 50
%lds_arr = OpTypeArray %uint %c64
%_ptr_WG_lds = OpTypePointer Workgroup %lds_arr
%_ptr_WG_uint = OpTypePointer Workgroup %uint
%lds = OpVariable %_ptr_WG_lds Workgroup
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
%ptr = OpAccessChain %_ptr_WG_uint %lds %c3
%dummy = OpAtomicSMin %uint %ptr %c2 %c0 %c50
OpReturn
OpFunctionEnd
"""
        interp = LdsAtomicMinI32Interp(spvasm)
        final_lds, _ = interp.run(seed_lds={3: 20})
        self.assertEqual(final_lds[3], 20)

    def test_ds_min_i32_negative_vs_positive(self):
        # Initial 10, lane provides -5 (0xFFFFFFFB) -> final LDS is -5
        spvasm = """
OpCapability Shader
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main"
OpExecutionMode %main LocalSize 1 1 1
%uint = OpTypeInt 32 0
%c64 = OpConstant %uint 64
%c2 = OpConstant %uint 2
%c0 = OpConstant %uint 0
%c5 = OpConstant %uint 5
%c_neg5 = OpConstant %uint 0xFFFFFFFB
%lds_arr = OpTypeArray %uint %c64
%_ptr_WG_lds = OpTypePointer Workgroup %lds_arr
%_ptr_WG_uint = OpTypePointer Workgroup %uint
%lds = OpVariable %_ptr_WG_lds Workgroup
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
%ptr = OpAccessChain %_ptr_WG_uint %lds %c5
%dummy = OpAtomicSMin %uint %ptr %c2 %c0 %c_neg5
OpReturn
OpFunctionEnd
"""
        interp = LdsAtomicMinI32Interp(spvasm)
        final_lds, _ = interp.run(seed_lds={5: 10})
        self.assertEqual(final_lds[5], 0xFFFFFFFB)
        self.assertEqual(to_signed_i32(final_lds[5]), -5)

    def test_ds_min_i32_negative_vs_negative(self):
        # Initial -10 (0xFFFFFFF6), lane provides -30 (0xFFFFFFE2) -> final LDS is -30
        spvasm = """
OpCapability Shader
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main"
OpExecutionMode %main LocalSize 1 1 1
%uint = OpTypeInt 32 0
%c64 = OpConstant %uint 64
%c2 = OpConstant %uint 2
%c0 = OpConstant %uint 0
%c1 = OpConstant %uint 1
%c_neg30 = OpConstant %uint 0xFFFFFFE2
%lds_arr = OpTypeArray %uint %c64
%_ptr_WG_lds = OpTypePointer Workgroup %lds_arr
%_ptr_WG_uint = OpTypePointer Workgroup %uint
%lds = OpVariable %_ptr_WG_lds Workgroup
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
%ptr = OpAccessChain %_ptr_WG_uint %lds %c1
%dummy = OpAtomicSMin %uint %ptr %c2 %c0 %c_neg30
OpReturn
OpFunctionEnd
"""
        interp = LdsAtomicMinI32Interp(spvasm)
        final_lds, _ = interp.run(seed_lds={1: 0xFFFFFFF6})
        self.assertEqual(final_lds[1], 0xFFFFFFE2)
        self.assertEqual(to_signed_i32(final_lds[1]), -30)

    def test_ds_min_i32_with_byte_offset(self):
        # Address calculation: base byte 16 + offset 8 = 24 bytes -> dword index 6
        spvasm = """
OpCapability Shader
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main"
OpExecutionMode %main LocalSize 1 1 1
%uint = OpTypeInt 32 0
%c128 = OpConstant %uint 128
%c2 = OpConstant %uint 2
%c0 = OpConstant %uint 0
%c16 = OpConstant %uint 16
%c8 = OpConstant %uint 8
%c_shift2 = OpConstant %uint 2
%c_mask127 = OpConstant %uint 127
%c_neg1 = OpConstant %uint 0xFFFFFFFF
%lds_arr = OpTypeArray %uint %c128
%_ptr_WG_lds = OpTypePointer Workgroup %lds_arr
%_ptr_WG_uint = OpTypePointer Workgroup %uint
%lds = OpVariable %_ptr_WG_lds Workgroup
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
%s1 = OpIAdd %uint %c16 %c8
%i = OpShiftRightLogical %uint %s1 %c_shift2
%m = OpBitwiseAnd %uint %i %c_mask127
%ptr = OpAccessChain %_ptr_WG_uint %lds %m
%dummy = OpAtomicSMin %uint %ptr %c2 %c0 %c_neg1
OpReturn
OpFunctionEnd
"""
        interp = LdsAtomicMinI32Interp(spvasm)
        final_lds, _ = interp.run(seed_lds={6: 100})
        # (16 + 8) >> 2 = 6, min(100, -1) = -1 (0xFFFFFFFF)
        self.assertEqual(final_lds[6], 0xFFFFFFFF)


class TestLdsBankConflictsAndAddressClash(unittest.TestCase):
    """Verifies SIMD multi-lane bank conflicts, shared address clashes, and reduction invariance."""

    def test_bank_index_mapping(self):
        # 32 banks, each 4 bytes (1 dword)
        for i in range(32):
            byte_addr = i * 4
            self.assertEqual(reference_lds_bank(byte_addr), i)

        # Dwords 32..63 wrap to banks 0..31
        for i in range(32):
            byte_addr = (32 + i) * 4
            self.assertEqual(reference_lds_bank(byte_addr), i)

        # Stride-32 clash: byte addresses i * 128 (dwords i * 32) all hit bank 0
        for i in range(32):
            byte_addr = i * 32 * 4
            self.assertEqual(reference_lds_bank(byte_addr), 0)

        # Stride-16: byte addresses i * 64 (dwords i * 16) hit banks 0 and 16
        for i in range(32):
            bank = reference_lds_bank(i * 16 * 4)
            self.assertIn(bank, (0, 16))

    def test_simd32_shared_address_clash_reduction(self):
        # All 32 lanes in a wave atomically reduce into the EXACT same LDS slot (index 7).
        # Initial value in LDS[7] is +500.
        # Lane i supplies 200 - i * 20 (ranging from +200 down to -420).
        # Expected minimum = -420 (0xFFFFFE5C).
        spvasm = """
OpCapability Shader
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main"
OpExecutionMode %main LocalSize 32 1 1
%uint = OpTypeInt 32 0
%c64 = OpConstant %uint 64
%c2 = OpConstant %uint 2
%c0 = OpConstant %uint 0
%c7_slot = OpConstant %uint 7
%lds_arr = OpTypeArray %uint %c64
%_ptr_WG_lds = OpTypePointer Workgroup %lds_arr
%_ptr_WG_uint = OpTypePointer Workgroup %uint
%lds = OpVariable %_ptr_WG_lds Workgroup
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
%ptr = OpAccessChain %_ptr_WG_uint %lds %c7_slot
%dummy = OpAtomicSMin %uint %ptr %c2 %c0 %lane_val
OpReturn
OpFunctionEnd
"""
        interp = LdsAtomicMinI32Interp(spvasm)
        lane_inputs = []
        expected_min = 500
        for lidx in range(32):
            val_s = 200 - lidx * 20
            expected_min = min(expected_min, val_s)
            lane_inputs.append({"%lane_val": to_unsigned_u32(val_s)})

        final_lds, _ = interp.run(num_invocations=32, seed_lds={7: 500}, lane_inputs=lane_inputs)
        self.assertEqual(final_lds[7], to_unsigned_u32(expected_min))
        self.assertEqual(to_signed_i32(final_lds[7]), -420)

    def test_simd32_bank_conflict_stride32(self):
        # 32 lanes each access distinct dwords that map to the SAME bank (bank 0):
        # dword indices: i * 32 for i in 0..31 (dwords 0, 32, 64, ..., 992).
        # In hardware, this causes a 32-way serialized bank conflict.
        # Each lane performs ds_min_i32 independently on its own dword.
        spvasm = """
OpCapability Shader
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main"
OpExecutionMode %main LocalSize 32 1 1
%uint = OpTypeInt 32 0
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
%ptr = OpAccessChain %_ptr_WG_uint %lds %lane_slot
%dummy = OpAtomicSMin %uint %ptr %c2 %c0 %lane_val
OpReturn
OpFunctionEnd
"""
        interp = LdsAtomicMinI32Interp(spvasm)
        seed_lds = {}
        lane_inputs = []
        expected_results = {}

        for lidx in range(32):
            slot = lidx * 32
            init_s = 1000 + lidx
            seed_lds[slot] = to_unsigned_u32(init_s)
            val_s = 50 - lidx * 100  # for lane 0: 50; for lane 1: -50; for lane 31: -3050
            lane_inputs.append({
                "%lane_slot": slot,
                "%lane_val": to_unsigned_u32(val_s)
            })
            expected_results[slot] = min(init_s, val_s)

        final_lds, _ = interp.run(num_invocations=32, seed_lds=seed_lds, lane_inputs=lane_inputs)
        for slot, exp_val in expected_results.items():
            self.assertEqual(to_signed_i32(final_lds[slot]), exp_val)

    def test_simd32_two_way_bank_conflict_stride16(self):
        # 32 lanes access addresses with stride 16 dwords ((i * 16) % 64),
        # causing 2-way bank conflicts on banks 0 and 16.
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
%ptr = OpAccessChain %_ptr_WG_uint %lds %lane_slot
%dummy = OpAtomicSMin %uint %ptr %c2 %c0 %lane_val
OpReturn
OpFunctionEnd
"""
        interp = LdsAtomicMinI32Interp(spvasm)
        seed_lds = {0: 500, 16: 500, 32: 500, 48: 500}
        lane_inputs = []
        expected = dict(seed_lds)

        for lidx in range(32):
            slot = (lidx * 16) % 64
            val_s = 100 - lidx * 25
            expected[slot] = min(expected[slot], val_s)
            lane_inputs.append({
                "%lane_slot": slot,
                "%lane_val": to_unsigned_u32(val_s)
            })

        final_lds, _ = interp.run(num_invocations=32, seed_lds=seed_lds, lane_inputs=lane_inputs)
        for slot in (0, 16, 32, 48):
            self.assertEqual(to_signed_i32(final_lds[slot]), expected[slot])

    def test_address_clash_lane_permutation_invariance(self):
        # 16 lanes clash on LDS[4] with mixed positive, negative, and extreme integers.
        # Tests that ANY lane execution order yields the exact same signed minimum.
        spvasm = """
OpCapability Shader
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main"
OpExecutionMode %main LocalSize 16 1 1
%uint = OpTypeInt 32 0
%c32 = OpConstant %uint 32
%c2 = OpConstant %uint 2
%c0 = OpConstant %uint 0
%c4_slot = OpConstant %uint 4
%lds_arr = OpTypeArray %uint %c32
%_ptr_WG_lds = OpTypePointer Workgroup %lds_arr
%_ptr_WG_uint = OpTypePointer Workgroup %uint
%lds = OpVariable %_ptr_WG_lds Workgroup
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
%ptr = OpAccessChain %_ptr_WG_uint %lds %c4_slot
%dummy = OpAtomicSMin %uint %ptr %c2 %c0 %lane_val
OpReturn
OpFunctionEnd
"""
        interp = LdsAtomicMinI32Interp(spvasm)
        raw_vals = [50, -12, 85, -999, 0, -4, 300, -999, 12, -500, -1, 40, -800, -2, -999, 15]
        lane_inputs = [{"%lane_val": to_unsigned_u32(v)} for v in raw_vals]
        global_min_s = min([100] + raw_vals)
        self.assertEqual(global_min_s, -999)
        expected_u = to_unsigned_u32(-999)

        orders = [
            list(range(16)),  # Forward
            list(range(15, -1, -1)),  # Reverse
            [0, 2, 4, 6, 8, 10, 12, 14, 1, 3, 5, 7, 9, 11, 13, 15],  # Even-first
            [1, 3, 5, 7, 9, 11, 13, 15, 0, 2, 4, 6, 8, 10, 12, 14],  # Odd-first
            [7, 2, 11, 0, 15, 3, 8, 1, 14, 4, 10, 6, 13, 5, 12, 9],  # Arbitrary permutation
        ]

        for order in orders:
            with self.subTest(order=order[:4]):
                final_lds, _ = interp.run(
                    num_invocations=16,
                    seed_lds={4: 100},
                    lane_inputs=lane_inputs,
                    lane_order=order,
                )
                self.assertEqual(final_lds[4], expected_u)

    def test_workgroup_wave64_two_bucket_clash(self):
        # 64-lane workgroup (Wave64):
        # Even lanes clash on LDS[0], odd lanes clash on LDS[1].
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
%ptr = OpAccessChain %_ptr_WG_uint %lds %bucket
%dummy = OpAtomicSMin %uint %ptr %c2 %c0 %lane_val
OpReturn
OpFunctionEnd
"""
        interp = LdsAtomicMinI32Interp(spvasm)
        even_min_s = 1000
        odd_min_s = 2000
        lane_inputs = []

        for lidx in range(64):
            bucket = lidx % 2
            if bucket == 0:
                val_s = 100 - lidx * 15
                even_min_s = min(even_min_s, val_s)
            else:
                val_s = 200 - lidx * 25
                odd_min_s = min(odd_min_s, val_s)
            lane_inputs.append({
                "%bucket": bucket,
                "%lane_val": to_unsigned_u32(val_s)
            })

        final_lds, _ = interp.run(
            num_invocations=64,
            seed_lds={0: 1000, 1: 2000},
            lane_inputs=lane_inputs
        )
        self.assertEqual(to_signed_i32(final_lds[0]), even_min_s)
        self.assertEqual(to_signed_i32(final_lds[1]), odd_min_s)

    def test_multi_bank_conflict_free_parallel_minimum(self):
        # 32 lanes target 32 consecutive dwords (0..31, one per bank, zero bank conflicts).
        spvasm = """
OpCapability Shader
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main"
OpExecutionMode %main LocalSize 32 1 1
%uint = OpTypeInt 32 0
%c32 = OpConstant %uint 32
%c2 = OpConstant %uint 2
%c0 = OpConstant %uint 0
%lds_arr = OpTypeArray %uint %c32
%_ptr_WG_lds = OpTypePointer Workgroup %lds_arr
%_ptr_WG_uint = OpTypePointer Workgroup %uint
%lds = OpVariable %_ptr_WG_lds Workgroup
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
%ptr = OpAccessChain %_ptr_WG_uint %lds %lane_id
%dummy = OpAtomicSMin %uint %ptr %c2 %c0 %lane_val
OpReturn
OpFunctionEnd
"""
        interp = LdsAtomicMinI32Interp(spvasm)
        seed_lds = {}
        lane_inputs = []
        expected = {}

        for lidx in range(32):
            seed_s = 100 - lidx * 10
            seed_lds[lidx] = to_unsigned_u32(seed_s)
            val_s = -50 - lidx * 5
            lane_inputs.append({
                "%lane_id": lidx,
                "%lane_val": to_unsigned_u32(val_s)
            })
            expected[lidx] = min(seed_s, val_s)

        final_lds, _ = interp.run(num_invocations=32, seed_lds=seed_lds, lane_inputs=lane_inputs)
        for lidx in range(32):
            self.assertEqual(to_signed_i32(final_lds[lidx]), expected[lidx])

    def test_simd_active_lane_masking(self):
        # 8 lanes, but exec_mask is 0b00100101 (only lanes 0, 2, 5 active).
        # Inactive lanes must not modify LDS.
        spvasm = """
OpCapability Shader
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main"
OpExecutionMode %main LocalSize 8 1 1
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
OpSelectionMerge %merge None
OpBranchConditional %exec_active %do_atomic %merge
%do_atomic = OpLabel
%ptr = OpAccessChain %_ptr_WG_uint %lds %c0_slot
%dummy = OpAtomicSMin %uint %ptr %c2 %c0 %lane_val
OpBranch %merge
%merge = OpLabel
OpReturn
OpFunctionEnd
"""
        interp = LdsAtomicMinI32Interp(spvasm)
        lane_vals = [-10, -999999, -50, -999999, -999999, -30, -999999, -999999]
        lane_inputs = [{"%lane_val": to_unsigned_u32(v)} for v in lane_vals]

        final_lds, _ = interp.run(
            num_invocations=8,
            seed_lds={0: 100},
            lane_inputs=lane_inputs,
            exec_mask=0b00100101
        )
        # Active lanes: 0 (-10), 2 (-50), 5 (-30). Initial: 100. Min = -50.
        self.assertEqual(to_signed_i32(final_lds[0]), -50)


class TestTwosComplementSignedBoundaries(unittest.TestCase):
    """Verifies two's complement boundary values, extremes, and address wrapping."""

    def test_int32_min_vs_int32_max_signed_vs_unsigned_extremes(self):
        # INT32_MIN (0x80000000 = -2147483648) vs INT32_MAX (0x7FFFFFFF = +2147483647).
        # Signed: min is 0x80000000 (-2147483648).
        # Unsigned: min is 0x7FFFFFFF (2147483647).
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
%c_int_min = OpConstant %uint 0x80000000
%lds_arr = OpTypeArray %uint %c16
%_ptr_WG_lds = OpTypePointer Workgroup %lds_arr
%_ptr_WG_uint = OpTypePointer Workgroup %uint
%lds = OpVariable %_ptr_WG_lds Workgroup
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
%ptr = OpAccessChain %_ptr_WG_uint %lds %c0_slot
%dummy = OpAtomicSMin %uint %ptr %c2 %c0 %c_int_min
OpReturn
OpFunctionEnd
"""
        interp = LdsAtomicMinI32Interp(spvasm)
        final_lds, _ = interp.run(seed_lds={0: UINT32_INT32_MAX})
        self.assertEqual(final_lds[0], UINT32_INT32_MIN)
        self.assertEqual(to_signed_i32(final_lds[0]), INT32_MIN_SIGNED)

    def test_minus_one_vs_zero(self):
        # -1 (0xFFFFFFFF) vs 0 (0x00000000). Signed min is -1 (0xFFFFFFFF).
        spvasm = """
OpCapability Shader
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main"
OpExecutionMode %main LocalSize 1 1 1
%uint = OpTypeInt 32 0
%c16 = OpConstant %uint 16
%c2 = OpConstant %uint 2
%c0 = OpConstant %uint 0
%c1_slot = OpConstant %uint 1
%c_minus1 = OpConstant %uint 0xFFFFFFFF
%lds_arr = OpTypeArray %uint %c16
%_ptr_WG_lds = OpTypePointer Workgroup %lds_arr
%_ptr_WG_uint = OpTypePointer Workgroup %uint
%lds = OpVariable %_ptr_WG_lds Workgroup
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
%ptr = OpAccessChain %_ptr_WG_uint %lds %c1_slot
%dummy = OpAtomicSMin %uint %ptr %c2 %c0 %c_minus1
OpReturn
OpFunctionEnd
"""
        interp = LdsAtomicMinI32Interp(spvasm)
        final_lds, _ = interp.run(seed_lds={1: 0})
        self.assertEqual(final_lds[1], 0xFFFFFFFF)
        self.assertEqual(to_signed_i32(final_lds[1]), -1)

    def test_int32_min_vs_int32_min_plus_one(self):
        # -2147483648 (0x80000000) vs -2147483647 (0x80000001). Signed min is 0x80000000.
        s_min = reference_atomic_smin(0x80000000, 0x80000001)
        self.assertEqual(s_min, 0x80000000)
        s_min_rev = reference_atomic_smin(0x80000001, 0x80000000)
        self.assertEqual(s_min_rev, 0x80000000)

    def test_int32_max_vs_int32_max_minus_one(self):
        # +2147483647 (0x7FFFFFFF) vs +2147483646 (0x7FFFFFFE). Signed min is 0x7FFFFFFE.
        s_min = reference_atomic_smin(0x7FFFFFFF, 0x7FFFFFFE)
        self.assertEqual(s_min, 0x7FFFFFFE)

    def test_signed_boundaries_with_wrapping_byte_offsets(self):
        # Hardware wrapping around 2^32 boundary:
        # Byte address 0xFFFFFFFC (-4) + offset 8 = 4 (dword 1).
        # Byte address 0xFFFFFFFC + offset 4 = 0 (dword 0).
        lds_dwords = 64
        self.assertEqual(reference_lds_index(0xFFFFFFFC, 8, lds_dwords), 1)
        self.assertEqual(reference_lds_index(0xFFFFFFFC, 4, lds_dwords), 0)

        spvasm = """
OpCapability Shader
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main"
OpExecutionMode %main LocalSize 1 1 1
%uint = OpTypeInt 32 0
%c64 = OpConstant %uint 64
%c2 = OpConstant %uint 2
%c0 = OpConstant %uint 0
%c_base = OpConstant %uint 0xFFFFFFFC
%c_off8 = OpConstant %uint 8
%c_shift2 = OpConstant %uint 2
%c_mask63 = OpConstant %uint 63
%c_neg42 = OpConstant %uint 0xFFFFFFD6
%lds_arr = OpTypeArray %uint %c64
%_ptr_WG_lds = OpTypePointer Workgroup %lds_arr
%_ptr_WG_uint = OpTypePointer Workgroup %uint
%lds = OpVariable %_ptr_WG_lds Workgroup
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
%sum = OpIAdd %uint %c_base %c_off8
%didx = OpShiftRightLogical %uint %sum %c_shift2
%wrapped = OpBitwiseAnd %uint %didx %c_mask63
%ptr = OpAccessChain %_ptr_WG_uint %lds %wrapped
%dummy = OpAtomicSMin %uint %ptr %c2 %c0 %c_neg42
OpReturn
OpFunctionEnd
"""
        interp = LdsAtomicMinI32Interp(spvasm)
        final_lds, _ = interp.run(seed_lds={1: 100})
        # (0xFFFFFFFC + 8) & 0xFFFFFFFF = 4 -> dword 1
        # min(100, -42) = -42 (0xFFFFFFD6)
        self.assertEqual(to_signed_i32(final_lds[1]), -42)


class TestSpirvAtomicSMinReturningAndRetention(unittest.TestCase):
    """Verifies returning form ds_min_rtn_i32 pre-atomic value retention and intermediate sequence."""

    def test_ds_min_rtn_i32_single_lane_retention(self):
        # Initial LDS[2] = 50. Lane supplies -20 (0xFFFFFFEC).
        # vdst receives pre-atomic value 50.
        # LDS[2] becomes -20.
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
%c2_slot = OpConstant %uint 2
%c_neg20 = OpConstant %uint 0xFFFFFFEC
%lds_arr = OpTypeArray %uint %c64
%_ptr_WG_lds = OpTypePointer Workgroup %lds_arr
%_ptr_WG_uint = OpTypePointer Workgroup %uint
%lds = OpVariable %_ptr_WG_lds Workgroup
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
%ptr = OpAccessChain %_ptr_WG_uint %lds %c2_slot
%old = OpAtomicSMin %uint %ptr %c2 %c0 %c_neg20
OpStore %out %old
OpReturn
OpFunctionEnd
"""
        interp = LdsAtomicMinI32Interp(spvasm)
        final_lds, lane_outs = interp.run(seed_lds={2: 50})
        self.assertEqual(to_signed_i32(final_lds[2]), -20)
        self.assertEqual(lane_outs[0]["%out"], 50)

    def test_ds_min_rtn_i32_single_lane_no_change_retention(self):
        # Initial LDS[2] = -20 (0xFFFFFFEC). Lane supplies +50.
        # vdst receives pre-atomic value -20.
        # LDS[2] remains -20.
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
%c2_slot = OpConstant %uint 2
%c50 = OpConstant %uint 50
%lds_arr = OpTypeArray %uint %c64
%_ptr_WG_lds = OpTypePointer Workgroup %lds_arr
%_ptr_WG_uint = OpTypePointer Workgroup %uint
%lds = OpVariable %_ptr_WG_lds Workgroup
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
%ptr = OpAccessChain %_ptr_WG_uint %lds %c2_slot
%old = OpAtomicSMin %uint %ptr %c2 %c0 %c50
OpStore %out %old
OpReturn
OpFunctionEnd
"""
        interp = LdsAtomicMinI32Interp(spvasm)
        final_lds, lane_outs = interp.run(seed_lds={2: 0xFFFFFFEC})
        self.assertEqual(final_lds[2], 0xFFFFFFEC)
        self.assertEqual(lane_outs[0]["%out"], 0xFFFFFFEC)
        self.assertEqual(to_signed_i32(lane_outs[0]["%out"]), -20)

    def test_ds_min_rtn_i32_multi_lane_monotonic_intermediate_retention(self):
        # 8 lanes clash on LDS[0] using returning form.
        # Initial value in LDS[0] = 100.
        # Sequence of lane inputs: [80, 90, 60, -10, 40, -50, 0, -100]
        #
        # Step-by-step intermediate state:
        # Lane 0: input 80. Pre-op is 100. LDS becomes min(100, 80) = 80.
        # Lane 1: input 90. Pre-op is 80. LDS becomes min(80, 90) = 80.
        # Lane 2: input 60. Pre-op is 80. LDS becomes min(80, 60) = 60.
        # Lane 3: input -10. Pre-op is 60. LDS becomes min(60, -10) = -10.
        # Lane 4: input 40. Pre-op is -10. LDS becomes min(-10, 40) = -10.
        # Lane 5: input -50. Pre-op is -10. LDS becomes min(-10, -50) = -50.
        # Lane 6: input 0. Pre-op is -50. LDS becomes min(-50, 0) = -50.
        # Lane 7: input -100. Pre-op is -50. LDS becomes min(-50, -100) = -100.
        #
        # Expected outputs per lane: [100, 80, 80, 60, -10, -10, -50, -50]
        # Final LDS[0]: -100.
        spvasm = """
OpCapability Shader
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main" %out
OpExecutionMode %main LocalSize 8 1 1
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
%old = OpAtomicSMin %uint %ptr %c2 %c0 %lane_val
OpStore %out %old
OpReturn
OpFunctionEnd
"""
        interp = LdsAtomicMinI32Interp(spvasm)
        seq_inputs = [80, 90, 60, -10, 40, -50, 0, -100]
        lane_inputs = [{"%lane_val": to_unsigned_u32(v)} for v in seq_inputs]

        final_lds, lane_outs = interp.run(
            num_invocations=8,
            seed_lds={0: 100},
            lane_inputs=lane_inputs,
        )

        expected_ret_signed = [100, 80, 80, 60, -10, -10, -50, -50]
        actual_ret_signed = [to_signed_i32(o["%out"]) for o in lane_outs]
        self.assertEqual(actual_ret_signed, expected_ret_signed)
        self.assertEqual(to_signed_i32(final_lds[0]), -100)

        # Confirm monotonicity: each returned value is <= the previous
        for k in range(1, len(actual_ret_signed)):
            self.assertLessEqual(actual_ret_signed[k], actual_ret_signed[k - 1])


class TestNegativeControlsAndErrors(unittest.TestCase):
    """Verifies bounds enforcement, invalid operands, and error conditions."""

    def test_out_of_bounds_lds_access_raises_bounds_error(self):
        # Attempt to access dword index 32 on a 32-dword LDS array (valid: 0..31)
        spvasm = """
OpCapability Shader
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main"
OpExecutionMode %main LocalSize 1 1 1
%uint = OpTypeInt 32 0
%c32 = OpConstant %uint 32
%c2 = OpConstant %uint 2
%c0 = OpConstant %uint 0
%c32_bad = OpConstant %uint 32
%lds_arr = OpTypeArray %uint %c32
%_ptr_WG_lds = OpTypePointer Workgroup %lds_arr
%_ptr_WG_uint = OpTypePointer Workgroup %uint
%lds = OpVariable %_ptr_WG_lds Workgroup
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
%ptr = OpAccessChain %_ptr_WG_uint %lds %c32_bad
%dummy = OpAtomicSMin %uint %ptr %c2 %c0 %c0
OpReturn
OpFunctionEnd
"""
        interp = LdsAtomicMinI32Interp(spvasm)
        with self.assertRaises(WorkgroupBoundsError):
            interp.run()

    def test_unsupported_spirv_header_opcode_raises(self):
        spvasm = """
OpCapability Shader
OpMemoryModel Logical GLSL450
OpUnknownHeaderOpcode
"""
        with self.assertRaises(UnsupportedConstruct):
            LdsAtomicMinI32Interp(spvasm)

    def test_unsupported_spirv_body_opcode_raises(self):
        spvasm = """
OpCapability Shader
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main"
OpExecutionMode %main LocalSize 1 1 1
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
OpNonExistentOpcode %uint
OpReturn
OpFunctionEnd
"""
        interp = LdsAtomicMinI32Interp(spvasm)
        with self.assertRaises(UnsupportedConstruct):
            interp.run()

    def test_invalid_localsize_product_exceeds_1024(self):
        spvasm = """
OpCapability Shader
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main"
OpExecutionMode %main LocalSize 1025 1 1
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
OpReturn
OpFunctionEnd
"""
        with self.assertRaises(WorkgroupBoundsError):
            LdsAtomicMinI32Interp(spvasm)

    def test_invalid_localsize_zero_dimension(self):
        spvasm = """
OpCapability Shader
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main"
OpExecutionMode %main LocalSize 0 1 1
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
OpReturn
OpFunctionEnd
"""
        with self.assertRaises(WorkgroupBoundsError):
            LdsAtomicMinI32Interp(spvasm)

    def test_unsupported_non_32bit_int(self):
        spvasm = """
OpCapability Shader
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main"
OpExecutionMode %main LocalSize 1 1 1
%int64 = OpTypeInt 64 0
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
OpReturn
OpFunctionEnd
"""
        with self.assertRaises(UnsupportedConstruct):
            LdsAtomicMinI32Interp(spvasm)

    def test_shift_right_amount_out_of_range(self):
        spvasm = """
OpCapability Shader
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main"
OpExecutionMode %main LocalSize 1 1 1
%uint = OpTypeInt 32 0
%c32 = OpConstant %uint 32
%c100 = OpConstant %uint 100
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
%res = OpShiftRightLogical %uint %c100 %c32
OpReturn
OpFunctionEnd
"""
        interp = LdsAtomicMinI32Interp(spvasm)
        with self.assertRaises(UnsupportedConstruct):
            interp.run()

    def test_shift_left_amount_out_of_range(self):
        spvasm = """
OpCapability Shader
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main"
OpExecutionMode %main LocalSize 1 1 1
%uint = OpTypeInt 32 0
%c64 = OpConstant %uint 64
%c100 = OpConstant %uint 100
%void = OpTypeVoid
%fn = OpTypeFunction %void
%main = OpFunction %void None %fn
%entry = OpLabel
%res = OpShiftLeftLogical %uint %c100 %c64
OpReturn
OpFunctionEnd
"""
        interp = LdsAtomicMinI32Interp(spvasm)
        with self.assertRaises(UnsupportedConstruct):
            interp.run()


class TestTranslatorSignedMinIntegration(unittest.TestCase):
    """Verifies that tools/rdna2spirv.py emits conforming SPIR-V for ds_min_i32 opcodes."""

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

    def test_lds_atomics_table_contains_ds_min_i32(self):
        self.assertIn("ds_min_i32", rdna2spirv.Gen.LDS_ATOMICS)
        self.assertEqual(rdna2spirv.Gen.LDS_ATOMICS["ds_min_i32"], "OpAtomicSMin")

    def test_ds_min_i32_non_returning_emission(self):
        gen = self._make_mock_gen(stage="cs")
        # ds_min_i32 v0, v1, offset:8 (args: addr=v0, data=v1)
        gen.lds_op("ds_min_i32", [{"r": 0}, {"r": 1}], ["offset:8"])
        self.assertTrue(gen.uses_lds)
        emitted_str = "\n".join(gen.emitted)

        self.assertIn("OpBranchConditional %exec_active", emitted_str)
        self.assertIn("OpIAdd %uint %v_0 %c8", emitted_str)
        self.assertIn("OpShiftRightLogical %uint", emitted_str)
        self.assertIn("OpBitwiseAnd %uint", emitted_str)
        self.assertIn("OpAccessChain %_ptr_WG_uint %lds", emitted_str)
        self.assertIn("OpAtomicSMin %uint", emitted_str)
        self.assertIn("%c2 %c0 %v_1", emitted_str)
        # Non-returning form must not store any destination register
        self.assertEqual(len(gen.stored_dsts), 0)

    def test_ds_min_rtn_i32_returning_emission(self):
        gen = self._make_mock_gen(stage="cs")
        # ds_min_rtn_i32 v7, v1, v2, offset:16 (args: dst=v7, addr=v1, data=v2)
        gen.lds_op("ds_min_rtn_i32", [{"r": 7}, {"r": 1}, {"r": 2}], ["offset:16"])
        self.assertTrue(gen.uses_lds)
        emitted_str = "\n".join(gen.emitted)

        self.assertIn("OpAtomicSMin %uint", emitted_str)
        self.assertIn("%c2 %c0 %v_2", emitted_str)
        self.assertEqual(len(gen.stored_dsts), 1)
        dest_reg, val_id = gen.stored_dsts[0]
        self.assertEqual(dest_reg["r"], 7)
        self.assertTrue(val_id.startswith("%t"))

    def test_ds_min_i32_outside_compute_raises(self):
        gen = self._make_mock_gen(stage="ps")
        with self.assertRaises(rdna2spirv.Unsupported):
            gen.lds_op("ds_min_i32", [{"r": 0}, {"r": 1}], [])


if __name__ == "__main__":
    unittest.main()
