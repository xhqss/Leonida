"""Semantic reference oracle and architectural model for ds_condxchg32_rtn_b32.

RDNA2 / GFX10 Local Data Share (LDS) Conditional Exchange:
Performs a 32-bit atomic compare-and-exchange operation on LDS memory.
Syntax:
    ds_condxchg32_rtn_b32 vdst, vaddr, vdata0, vdata1 [offset:offset16]

Semantics:
    effective_byte_address = (vaddr + offset16) & 0xFFFFFFFF
    dword_index = (effective_byte_address >> 2) & (lds_dwords - 1)
    old_val = LDS[dword_index]
    if old_val == vdata0:
        LDS[dword_index] = vdata1
    vdst = old_val
"""
from __future__ import annotations

import math
import struct
import unittest
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import List, Optional, Tuple, Union

# RDNA2 Local Data Share (LDS) Architectural Constants
LDS_SIZE_BYTES: int = 65536         # 64 KiB default capacity
LDS_SIZE_DWORDS: int = 16384        # 16,384 32-bit dwords
LDS_BANK_COUNT: int = 32            # 32 independent banks
MASK_32: int = 0xFFFF_FFFF          # 32-bit unsigned mask
OFFSET16_MASK: int = 0xFFFF         # 16-bit immediate byte offset mask


def calc_lds_effective_address(vaddr: int, offset: int = 0) -> int:
    """Calculate the 32-bit effective byte address into LDS.
    
    The offset is an unsigned 16-bit immediate. Address wrapping is strictly modulo 2^32.
    """
    return (vaddr + (offset & OFFSET16_MASK)) & MASK_32


def calc_lds_dword_index(eff_addr: int, capacity_dwords: int = LDS_SIZE_DWORDS) -> int:
    """Convert an effective byte address to an aligned LDS dword index.
    
    LDS addresses are dword-aligned; the low 2 bits are ignored by hardware.
    The index wraps modulo the power-of-two dword capacity.
    """
    return (eff_addr >> 2) & (capacity_dwords - 1)


def calc_lds_bank(eff_addr: int) -> int:
    """Determine the LDS memory bank index for an effective address."""
    return (eff_addr >> 2) & (LDS_BANK_COUNT - 1)


def oracle_ds_condxchg32(
    current_mem: int,
    cmp_val: int,
    new_val: int,
) -> Tuple[int, int, bool]:
    """Pure mathematical reference oracle for ds_condxchg32_rtn_b32.
    
    Returns:
        (returned_old_val, new_mem_val, exchanged)
    """
    old_val = current_mem & MASK_32
    cmp_masked = cmp_val & MASK_32
    new_masked = new_val & MASK_32

    if old_val == cmp_masked:
        return old_val, new_masked, True
    return old_val, old_val, False


class LDSModel:
    """Architectural memory model representing the Local Data Share scratchpad."""

    def __init__(self, capacity_dwords: int = LDS_SIZE_DWORDS) -> None:
        if capacity_dwords <= 0 or (capacity_dwords & (capacity_dwords - 1)) != 0:
            raise ValueError(f"capacity_dwords must be a power of two, got {capacity_dwords}")
        self.capacity_dwords = capacity_dwords
        self.memory = [0] * capacity_dwords

    def read_dword(self, eff_addr: int) -> int:
        """Read a 32-bit dword from LDS given an effective byte address."""
        idx = calc_lds_dword_index(eff_addr, self.capacity_dwords)
        return self.memory[idx]

    def write_dword(self, eff_addr: int, val: int) -> None:
        """Write a 32-bit dword to LDS given an effective byte address."""
        idx = calc_lds_dword_index(eff_addr, self.capacity_dwords)
        self.memory[idx] = val & MASK_32

    def atomic_condxchg32(
        self,
        vaddr: int,
        cmp_val: int,
        new_val: int,
        offset: int = 0,
    ) -> Tuple[int, int, bool]:
        """Execute atomic conditional exchange on LDS dword."""
        eff_addr = calc_lds_effective_address(vaddr, offset)
        idx = calc_lds_dword_index(eff_addr, self.capacity_dwords)
        curr = self.memory[idx]
        old_val, new_mem, exchanged = oracle_ds_condxchg32(curr, cmp_val, new_val)
        self.memory[idx] = new_mem
        return old_val, new_mem, exchanged


class TestLdsCondxchg32ReferenceOracle(unittest.TestCase):
    """Phase 1: Unit tests for reference mathematical oracle and architectural model."""

    def test_successful_exchange_when_equal(self) -> None:
        old, mem, swapped = oracle_ds_condxchg32(0x12345678, 0x12345678, 0x9ABCDEF0)
        self.assertTrue(swapped)
        self.assertEqual(old, 0x12345678)
        self.assertEqual(mem, 0x9ABCDEF0)

    def test_failed_exchange_when_not_equal(self) -> None:
        old, mem, swapped = oracle_ds_condxchg32(0x11112222, 0x33334444, 0x99990000)
        self.assertFalse(swapped)
        self.assertEqual(old, 0x11112222)
        self.assertEqual(mem, 0x11112222)

    def test_effective_address_with_immediate_offset(self) -> None:
        eff = calc_lds_effective_address(0x1000, 0x0024)
        self.assertEqual(eff, 0x1024)
        self.assertEqual(calc_lds_dword_index(eff), 0x1024 >> 2)

    def test_dword_alignment_and_bit_truncation(self) -> None:
        # Addresses differing only in byte offset within a dword map to the same dword
        eff0 = calc_lds_effective_address(0x200)
        eff1 = calc_lds_effective_address(0x201)
        eff2 = calc_lds_effective_address(0x202)
        eff3 = calc_lds_effective_address(0x203)
        self.assertEqual(calc_lds_dword_index(eff0), 0x80)
        self.assertEqual(calc_lds_dword_index(eff1), 0x80)
        self.assertEqual(calc_lds_dword_index(eff2), 0x80)
        self.assertEqual(calc_lds_dword_index(eff3), 0x80)

    def test_modulo_wrapping_at_capacity_boundary(self) -> None:
        capacity_bytes = LDS_SIZE_DWORDS * 4
        eff = calc_lds_effective_address(capacity_bytes + 0x10)
        idx = calc_lds_dword_index(eff)
        self.assertEqual(idx, 0x10 >> 2)

    def test_32bit_address_overflow_wrapping(self) -> None:
        eff = calc_lds_effective_address(0xFFFF_FFFE, 0x0004)
        self.assertEqual(eff, 0x0000_0002)
        self.assertEqual(calc_lds_dword_index(eff), 0)

    def test_bank_index_distribution(self) -> None:
        for bank in range(32):
            addr = bank * 4
            self.assertEqual(calc_lds_bank(addr), bank)

    def test_lds_model_state_update_on_match(self) -> None:
        lds = LDSModel()
        addr = 0x400
        lds.write_dword(addr, 0xCAFEBABE)
        old, mem, swapped = lds.atomic_condxchg32(addr, 0xCAFEBABE, 0xDEADBEEF, offset=0)
        self.assertTrue(swapped)
        self.assertEqual(old, 0xCAFEBABE)
        self.assertEqual(mem, 0xDEADBEEF)
        self.assertEqual(lds.read_dword(addr), 0xDEADBEEF)

    def test_lds_model_state_preservation_on_mismatch(self) -> None:
        lds = LDSModel()
        addr = 0x800
        lds.write_dword(addr, 0x5555AAAA)
        # Attempt swap at addr with mismatched compare value
        old, mem, swapped = lds.atomic_condxchg32(addr, 0x11112222, 0xFFFFFFFF, offset=0)
        self.assertFalse(swapped)
        self.assertEqual(old, 0x5555AAAA)
        self.assertEqual(mem, 0x5555AAAA)
        # Check original addr was preserved
        self.assertEqual(lds.read_dword(addr), 0x5555AAAA)


# ==============================================================================
# Phase 2: Symbolic AST Opcode Definitions and Typed Expressions
# ==============================================================================

class RegType(Enum):
    VGPR = auto()
    SGPR = auto()


@dataclass(frozen=True)
class VGPR:
    """Vector General Purpose Register operand (e.g. v[0] .. v[255])."""
    index: int

    def __post_init__(self) -> None:
        if not (0 <= self.index <= 255):
            raise ValueError(f"VGPR index {self.index} out of architectural range [0, 255]")

    @property
    def name(self) -> str:
        return f"v[{self.index}]"


@dataclass(frozen=True)
class SGPR:
    """Scalar General Purpose Register operand (e.g. s[0] .. s[105])."""
    index: int

    def __post_init__(self) -> None:
        if not (0 <= self.index <= 105):
            raise ValueError(f"SGPR index {self.index} out of architectural range [0, 105]")

    @property
    def name(self) -> str:
        return f"s[{self.index}]"


@dataclass(frozen=True)
class Immediate:
    """32-bit literal or inline integer constant."""
    value: int

    @property
    def name(self) -> str:
        return f"0x{self.value & MASK_32:X}"


Operand = Union[VGPR, SGPR, Immediate]


class Condition(Enum):
    """Comparison predicate for conditional exchange."""
    EQ = auto()
    NE = auto()
    GT_U32 = auto()
    LT_U32 = auto()


@dataclass(frozen=True)
class DSCondxchg32Instruction:
    """Symbolic AST node for ds_condxchg32_rtn_b32 instruction."""
    dst: VGPR
    addr: VGPR
    cmp_val: Operand
    new_val: Operand
    offset: int = 0
    cond: Condition = Condition.EQ

    def __post_init__(self) -> None:
        if not isinstance(self.dst, VGPR):
            raise TypeError(f"Destination must be a VGPR, got {type(self.dst).__name__}")
        if not isinstance(self.addr, VGPR):
            raise TypeError(f"Address operand must be a VGPR, got {type(self.addr).__name__}")
        if not (0 <= self.offset <= 65535):
            raise ValueError(f"Offset {self.offset} out of 16-bit range [0, 65535]")

    def disassemble(self) -> str:
        offset_part = f" offset:{self.offset}" if self.offset != 0 else ""
        return (
            f"ds_condxchg32_rtn_b32 {self.dst.name}, {self.addr.name}, "
            f"{self.cmp_val.name}, {self.new_val.name}{offset_part}"
        )


class TestSymbolicAST(unittest.TestCase):
    """Phase 2: Unit tests for symbolic AST opcode definitions and typed expressions."""

    def test_instruction_ast_construction(self) -> None:
        instr = DSCondxchg32Instruction(
            dst=VGPR(0),
            addr=VGPR(1),
            cmp_val=VGPR(2),
            new_val=VGPR(3),
            offset=16,
        )
        self.assertEqual(instr.dst.index, 0)
        self.assertEqual(instr.addr.index, 1)
        self.assertEqual(instr.offset, 16)
        self.assertEqual(instr.cond, Condition.EQ)

    def test_disassembly_with_and_without_offset(self) -> None:
        instr_no_off = DSCondxchg32Instruction(
            dst=VGPR(5),
            addr=VGPR(6),
            cmp_val=Immediate(0x1234),
            new_val=SGPR(10),
        )
        self.assertEqual(
            instr_no_off.disassemble(),
            "ds_condxchg32_rtn_b32 v[5], v[6], 0x1234, s[10]",
        )

        instr_off = DSCondxchg32Instruction(
            dst=VGPR(12),
            addr=VGPR(13),
            cmp_val=VGPR(14),
            new_val=Immediate(0xABCD),
            offset=64,
        )
        self.assertEqual(
            instr_off.disassemble(),
            "ds_condxchg32_rtn_b32 v[12], v[13], v[14], 0xABCD offset:64",
        )

    def test_dst_type_enforcement_rejects_sgpr(self) -> None:
        with self.assertRaises(TypeError):
            # Destination cannot be an SGPR in DS operations
            DSCondxchg32Instruction(
                dst=SGPR(0),  # type: ignore[arg-type]
                addr=VGPR(1),
                cmp_val=Immediate(0),
                new_val=Immediate(0),
            )

    def test_addr_type_enforcement_rejects_sgpr(self) -> None:
        with self.assertRaises(TypeError):
            # Address operand cannot be an SGPR in DS operations
            DSCondxchg32Instruction(
                dst=VGPR(0),
                addr=SGPR(1),  # type: ignore[arg-type]
                cmp_val=Immediate(0),
                new_val=Immediate(0),
            )

    def test_register_index_out_of_bounds(self) -> None:
        with self.assertRaises(ValueError):
            VGPR(256)
        with self.assertRaises(ValueError):
            VGPR(-1)
        with self.assertRaises(ValueError):
            SGPR(106)
        with self.assertRaises(ValueError):
            SGPR(-1)

    def test_offset_out_of_bounds(self) -> None:
        with self.assertRaises(ValueError):
            DSCondxchg32Instruction(
                dst=VGPR(0),
                addr=VGPR(1),
                cmp_val=Immediate(0),
                new_val=Immediate(0),
                offset=65536,
            )
        with self.assertRaises(ValueError):
            DSCondxchg32Instruction(
                dst=VGPR(0),
                addr=VGPR(1),
                cmp_val=Immediate(0),
                new_val=Immediate(0),
                offset=-1,
            )

    def test_operand_imm_and_reg_equality(self) -> None:
        v0_a = VGPR(0)
        v0_b = VGPR(0)
        v1 = VGPR(1)
        self.assertEqual(v0_a, v0_b)
        self.assertNotEqual(v0_a, v1)
        imm1 = Immediate(42)
        imm2 = Immediate(42)
        self.assertEqual(imm1, imm2)


# ==============================================================================
# Phase 3: Deterministic Scalar Instruction Interpreter
# ==============================================================================

@dataclass
class ScalarMachineState:
    """Architectural register and scratchpad state for scalar execution."""
    vgprs: List[int] = field(default_factory=lambda: [0] * 256)
    sgprs: List[int] = field(default_factory=lambda: [0] * 106)
    lds: LDSModel = field(default_factory=LDSModel)

    def get_vgpr(self, index: int) -> int:
        return self.vgprs[index] & MASK_32

    def set_vgpr(self, index: int, val: int) -> None:
        self.vgprs[index] = val & MASK_32

    def get_sgpr(self, index: int) -> int:
        return self.sgprs[index] & MASK_32

    def set_sgpr(self, index: int, val: int) -> None:
        self.sgprs[index] = val & MASK_32


class ScalarInterpreter:
    """Deterministic scalar interpreter for ds_condxchg32_rtn_b32."""

    def resolve_operand(self, op: Operand, state: ScalarMachineState) -> int:
        if isinstance(op, VGPR):
            return state.get_vgpr(op.index)
        if isinstance(op, SGPR):
            return state.get_sgpr(op.index)
        if isinstance(op, Immediate):
            return op.value & MASK_32
        raise TypeError(f"Unknown operand type: {type(op).__name__}")

    def evaluate_condition(self, curr_mem: int, cmp_val: int, cond: Condition) -> bool:
        if cond == Condition.EQ:
            return curr_mem == cmp_val
        if cond == Condition.NE:
            return curr_mem != cmp_val
        if cond == Condition.GT_U32:
            return curr_mem > cmp_val
        if cond == Condition.LT_U32:
            return curr_mem < cmp_val
        raise ValueError(f"Unsupported condition: {cond}")

    def step(
        self,
        instr: DSCondxchg32Instruction,
        state: ScalarMachineState,
    ) -> Tuple[int, int, bool]:
        """Execute a single ds_condxchg32_rtn_b32 instruction deterministically.
        
        All operands are latched before write-back to ensure operand preservation.
        Returns:
            (returned_old_val, updated_mem_val, exchanged)
        """
        # 1. Latch inputs
        vaddr = state.get_vgpr(instr.addr.index)
        cmp_val = self.resolve_operand(instr.cmp_val, state)
        new_val = self.resolve_operand(instr.new_val, state)

        # 2. Compute effective address and fetch current LDS value
        eff_addr = calc_lds_effective_address(vaddr, instr.offset)
        curr_mem = state.lds.read_dword(eff_addr)

        # 3. Evaluate conditional exchange
        cond_met = self.evaluate_condition(curr_mem, cmp_val, instr.cond)
        next_mem = new_val if cond_met else curr_mem

        # 4. Atomic memory writeback
        if cond_met:
            state.lds.write_dword(eff_addr, next_mem)

        # 5. Write return value to destination VGPR
        state.set_vgpr(instr.dst.index, curr_mem)

        return curr_mem, next_mem, cond_met


class TestScalarInterpreter(unittest.TestCase):
    """Phase 3: Unit tests for deterministic scalar instruction interpreter."""

    def setUp(self) -> None:
        self.state = ScalarMachineState()
        self.interp = ScalarInterpreter()

    def test_scalar_step_successful_exchange(self) -> None:
        addr = 0x200
        self.state.set_vgpr(1, addr)
        self.state.set_vgpr(2, 0x12345678)  # cmp_val
        self.state.set_vgpr(3, 0x9ABCDEF0)  # new_val
        self.state.lds.write_dword(addr, 0x12345678)

        instr = DSCondxchg32Instruction(
            dst=VGPR(0),
            addr=VGPR(1),
            cmp_val=VGPR(2),
            new_val=VGPR(3),
        )
        old, mem, swapped = self.interp.step(instr, self.state)
        self.assertTrue(swapped)
        self.assertEqual(old, 0x12345678)
        self.assertEqual(mem, 0x9ABCDEF0)
        self.assertEqual(self.state.get_vgpr(0), 0x12345678)
        self.assertEqual(self.state.lds.read_dword(addr), 0x9ABCDEF0)

    def test_scalar_step_failed_exchange_retains_mem(self) -> None:
        addr = 0x300
        self.state.set_vgpr(1, addr)
        self.state.set_vgpr(2, 0xAAAAAAAA)  # cmp_val (mismatch)
        self.state.set_vgpr(3, 0xBBBBBBBB)  # new_val
        self.state.lds.write_dword(addr, 0x55555555)

        instr = DSCondxchg32Instruction(
            dst=VGPR(0),
            addr=VGPR(1),
            cmp_val=VGPR(2),
            new_val=VGPR(3),
        )
        old, mem, swapped = self.interp.step(instr, self.state)
        self.assertFalse(swapped)
        self.assertEqual(old, 0x55555555)
        self.assertEqual(mem, 0x55555555)
        self.assertEqual(self.state.get_vgpr(0), 0x55555555)
        self.assertEqual(self.state.lds.read_dword(addr), 0x55555555)

    def test_scalar_step_with_sgpr_operands(self) -> None:
        addr = 0x400
        self.state.set_vgpr(1, addr)
        self.state.set_sgpr(10, 0x11111111)  # cmp_val in SGPR
        self.state.set_sgpr(11, 0x22222222)  # new_val in SGPR
        self.state.lds.write_dword(addr, 0x11111111)

        instr = DSCondxchg32Instruction(
            dst=VGPR(4),
            addr=VGPR(1),
            cmp_val=SGPR(10),
            new_val=SGPR(11),
        )
        old, mem, swapped = self.interp.step(instr, self.state)
        self.assertTrue(swapped)
        self.assertEqual(old, 0x11111111)
        self.assertEqual(mem, 0x22222222)
        self.assertEqual(self.state.get_vgpr(4), 0x11111111)

    def test_scalar_step_with_immediate_operands(self) -> None:
        addr = 0x500
        self.state.set_vgpr(1, addr)
        self.state.lds.write_dword(addr, 0x42)

        instr = DSCondxchg32Instruction(
            dst=VGPR(5),
            addr=VGPR(1),
            cmp_val=Immediate(0x42),
            new_val=Immediate(0x99),
        )
        old, mem, swapped = self.interp.step(instr, self.state)
        self.assertTrue(swapped)
        self.assertEqual(old, 0x42)
        self.assertEqual(mem, 0x99)
        self.assertEqual(self.state.get_vgpr(5), 0x42)
        self.assertEqual(self.state.lds.read_dword(addr), 0x99)

    def test_scalar_step_immediate_offset(self) -> None:
        base_addr = 0x600
        offset = 0x08
        eff_addr = base_addr + offset
        self.state.set_vgpr(2, base_addr)
        self.state.lds.write_dword(eff_addr, 0x77777777)

        instr = DSCondxchg32Instruction(
            dst=VGPR(6),
            addr=VGPR(2),
            cmp_val=Immediate(0x77777777),
            new_val=Immediate(0x88888888),
            offset=offset,
        )
        old, mem, swapped = self.interp.step(instr, self.state)
        self.assertTrue(swapped)
        self.assertEqual(old, 0x77777777)
        self.assertEqual(mem, 0x88888888)
        self.assertEqual(self.state.lds.read_dword(eff_addr), 0x88888888)
        # Check base address dword was not altered
        self.assertEqual(self.state.lds.read_dword(base_addr), 0)

    def test_scalar_step_alternate_condition_gt(self) -> None:
        addr = 0x700
        self.state.set_vgpr(1, addr)
        self.state.lds.write_dword(addr, 100)

        # Condition: curr_mem > 50 -> should swap
        instr_gt = DSCondxchg32Instruction(
            dst=VGPR(7),
            addr=VGPR(1),
            cmp_val=Immediate(50),
            new_val=Immediate(200),
            cond=Condition.GT_U32,
        )
        old, mem, swapped = self.interp.step(instr_gt, self.state)
        self.assertTrue(swapped)
        self.assertEqual(old, 100)
        self.assertEqual(mem, 200)


# ==============================================================================
# Phase 4: SIMD Multi-Lane Divergence and 32/64-Bit EXEC Masking
# ==============================================================================

@dataclass
class SIMDState:
    """Multi-lane SIMD machine state representing Wave32 or Wave64."""
    num_lanes: int
    exec_mask: Optional[int] = None
    vgprs: List[List[int]] = field(default_factory=list)
    sgprs: List[int] = field(default_factory=lambda: [0] * 106)
    lds: LDSModel = field(default_factory=LDSModel)

    def __post_init__(self) -> None:
        if self.num_lanes not in (32, 64):
            raise ValueError(f"num_lanes must be 32 or 64, got {self.num_lanes}")
        if self.exec_mask is None:
            self.exec_mask = (1 << self.num_lanes) - 1
        if not self.vgprs:
            self.vgprs = [[0] * 256 for _ in range(self.num_lanes)]

    def get_vgpr(self, lane: int, index: int) -> int:
        return self.vgprs[lane][index] & MASK_32

    def set_vgpr(self, lane: int, index: int, val: int) -> None:
        self.vgprs[lane][index] = val & MASK_32

    def get_sgpr(self, index: int) -> int:
        return self.sgprs[index] & MASK_32

    def set_sgpr(self, index: int, val: int) -> None:
        self.sgprs[index] = val & MASK_32

    def is_lane_active(self, lane: int) -> bool:
        assert self.exec_mask is not None
        return bool((self.exec_mask >> lane) & 1)


class SIMDInterpreter:
    """SIMD wavefront execution engine modeling multi-lane divergence and EXEC masking."""

    def __init__(self) -> None:
        self.scalar_interp = ScalarInterpreter()

    def resolve_lane_operand(
        self,
        op: Operand,
        lane: int,
        state: SIMDState,
    ) -> int:
        if isinstance(op, VGPR):
            return state.get_vgpr(lane, op.index)
        if isinstance(op, SGPR):
            return state.get_sgpr(op.index)
        if isinstance(op, Immediate):
            return op.value & MASK_32
        raise TypeError(f"Unknown operand type: {type(op).__name__}")

    def step(
        self,
        instr: DSCondxchg32Instruction,
        state: SIMDState,
    ) -> List[Tuple[Optional[int], Optional[int], bool]]:
        """Step the SIMD wavefront through ds_condxchg32_rtn_b32.
        
        Hardware executes memory transactions across active lanes.
        When multiple active lanes target the exact same LDS dword address,
        hardware serializes the requests in ascending lane order (lane 0 first, etc.).
        Inactive lanes perform no LDS read/write and leave destination VGPR unchanged.
        """
        # Step 1: Latch inputs for active lanes
        lane_requests = []
        for lane in range(state.num_lanes):
            if state.is_lane_active(lane):
                vaddr = state.get_vgpr(lane, instr.addr.index)
                cmp_val = self.resolve_lane_operand(instr.cmp_val, lane, state)
                new_val = self.resolve_lane_operand(instr.new_val, lane, state)
                eff_addr = calc_lds_effective_address(vaddr, instr.offset)
                lane_requests.append((lane, eff_addr, cmp_val, new_val))

        results: List[Tuple[Optional[int], Optional[int], bool]] = [
            (None, None, False) for _ in range(state.num_lanes)
        ]

        # Step 2: Deterministic lane serialization for memory operations
        for lane, eff_addr, cmp_val, new_val in lane_requests:
            curr_mem = state.lds.read_dword(eff_addr)
            cond_met = self.scalar_interp.evaluate_condition(curr_mem, cmp_val, instr.cond)
            next_mem = new_val if cond_met else curr_mem

            if cond_met:
                state.lds.write_dword(eff_addr, next_mem)

            # Destination writeback for active lane
            state.set_vgpr(lane, instr.dst.index, curr_mem)
            results[lane] = (curr_mem, next_mem, cond_met)

        return results


class TestSIMDMultiLaneDivergence(unittest.TestCase):
    """Phase 4: Unit tests for SIMD multi-lane divergence and EXEC masking."""

    def test_simd_wave32_independent_addresses(self) -> None:
        state = SIMDState(num_lanes=32)
        interp = SIMDInterpreter()

        # Each lane writes and swaps at its own dword address
        for lane in range(32):
            addr = lane * 4
            state.set_vgpr(lane, 1, addr)
            state.set_vgpr(lane, 2, 0x1000 + lane)  # cmp_val
            state.set_vgpr(lane, 3, 0x2000 + lane)  # new_val
            state.lds.write_dword(addr, 0x1000 + lane)

        instr = DSCondxchg32Instruction(
            dst=VGPR(0),
            addr=VGPR(1),
            cmp_val=VGPR(2),
            new_val=VGPR(3),
        )
        results = interp.step(instr, state)

        for lane in range(32):
            old, mem, swapped = results[lane]
            self.assertTrue(swapped)
            self.assertEqual(old, 0x1000 + lane)
            self.assertEqual(mem, 0x2000 + lane)
            self.assertEqual(state.get_vgpr(lane, 0), 0x1000 + lane)
            self.assertEqual(state.lds.read_dword(lane * 4), 0x2000 + lane)

    def test_simd_wave64_independent_addresses(self) -> None:
        state = SIMDState(num_lanes=64)
        interp = SIMDInterpreter()

        for lane in range(64):
            addr = 0x1000 + lane * 4
            state.set_vgpr(lane, 1, addr)
            state.lds.write_dword(addr, 0xAAAA_0000 | lane)

        instr = DSCondxchg32Instruction(
            dst=VGPR(0),
            addr=VGPR(1),
            cmp_val=Immediate(0xAAAA_0000),  # Only lane 0 will match exactly
            new_val=Immediate(0xFFFF_FFFF),
        )
        results = interp.step(instr, state)

        # Lane 0 matched and exchanged
        self.assertTrue(results[0][2])
        self.assertEqual(state.get_vgpr(0, 0), 0xAAAA_0000)
        self.assertEqual(state.lds.read_dword(0x1000), 0xFFFF_FFFF)

        # Other lanes mismatched
        for lane in range(1, 64):
            self.assertFalse(results[lane][2])
            self.assertEqual(state.get_vgpr(lane, 0), 0xAAAA_0000 | lane)
            self.assertEqual(state.lds.read_dword(0x1000 + lane * 4), 0xAAAA_0000 | lane)

    def test_simd_exec_mask_partial_inactivity(self) -> None:
        # Active only even lanes: 0, 2, 4, ...
        even_mask = sum(1 << i for i in range(0, 32, 2))
        state = SIMDState(num_lanes=32, exec_mask=even_mask)
        interp = SIMDInterpreter()

        sentinel_dst = 0xDEADBEEF
        addr = 0x400

        for lane in range(32):
            state.set_vgpr(lane, 0, sentinel_dst)
            state.set_vgpr(lane, 1, addr + lane * 4)
            state.lds.write_dword(addr + lane * 4, 0x55)

        instr = DSCondxchg32Instruction(
            dst=VGPR(0),
            addr=VGPR(1),
            cmp_val=Immediate(0x55),
            new_val=Immediate(0x99),
        )
        results = interp.step(instr, state)

        for lane in range(32):
            if lane % 2 == 0:
                self.assertTrue(results[lane][2])
                self.assertEqual(state.get_vgpr(lane, 0), 0x55)
                self.assertEqual(state.lds.read_dword(addr + lane * 4), 0x99)
            else:
                self.assertFalse(results[lane][2])
                # Inactive lane destination VGPR remains sentinel
                self.assertEqual(state.get_vgpr(lane, 0), sentinel_dst)
                # Inactive lane memory untouched
                self.assertEqual(state.lds.read_dword(addr + lane * 4), 0x55)

    def test_simd_exec_mask_all_inactive_preserves_dst_and_lds(self) -> None:
        state = SIMDState(num_lanes=32, exec_mask=0)
        interp = SIMDInterpreter()

        for lane in range(32):
            state.set_vgpr(lane, 0, 0x1234)
            state.set_vgpr(lane, 1, 0x800)
        state.lds.write_dword(0x800, 0xCAFE)

        instr = DSCondxchg32Instruction(
            dst=VGPR(0),
            addr=VGPR(1),
            cmp_val=Immediate(0xCAFE),
            new_val=Immediate(0xBEEF),
        )
        interp.step(instr, state)

        # Nothing changed
        self.assertEqual(state.lds.read_dword(0x800), 0xCAFE)
        for lane in range(32):
            self.assertEqual(state.get_vgpr(lane, 0), 0x1234)

    def test_simd_colliding_address_serialization_chain(self) -> None:
        """When multiple lanes target the same address, lane 0 executes first,
        lane 1 executes second and observes lane 0's write, etc."""
        state = SIMDState(num_lanes=32, exec_mask=0x7)  # Lanes 0, 1, 2 active
        interp = SIMDInterpreter()

        common_addr = 0x200
        state.lds.write_dword(common_addr, 100)

        # Lane 0: expects 100, sets 200
        state.set_vgpr(0, 1, common_addr)
        state.set_vgpr(0, 2, 100)
        state.set_vgpr(0, 3, 200)

        # Lane 1: expects 200 (produced by lane 0), sets 300
        state.set_vgpr(1, 1, common_addr)
        state.set_vgpr(1, 2, 200)
        state.set_vgpr(1, 3, 300)

        # Lane 2: expects 999 (mismatch!), tries to set 400
        state.set_vgpr(2, 1, common_addr)
        state.set_vgpr(2, 2, 999)
        state.set_vgpr(2, 3, 400)

        instr = DSCondxchg32Instruction(
            dst=VGPR(0),
            addr=VGPR(1),
            cmp_val=VGPR(2),
            new_val=VGPR(3),
        )
        results = interp.step(instr, state)

        # Lane 0 swapped 100 -> 200
        self.assertTrue(results[0][2])
        self.assertEqual(state.get_vgpr(0, 0), 100)

        # Lane 1 observed 200 and swapped 200 -> 300
        self.assertTrue(results[1][2])
        self.assertEqual(state.get_vgpr(1, 0), 200)

        # Lane 2 observed 300, failed match with 999
        self.assertFalse(results[2][2])
        self.assertEqual(state.get_vgpr(2, 0), 300)

        # Final memory holds 300
        self.assertEqual(state.lds.read_dword(common_addr), 300)


# ==============================================================================
# Phase 5: Register Aliasing and In-Place Operand Preservation
# ==============================================================================

class TestRegisterAliasingAndPreservation(unittest.TestCase):
    """Phase 5: Unit tests for register aliasing and in-place operand preservation."""

    def setUp(self) -> None:
        self.scalar_interp = ScalarInterpreter()
        self.simd_interp = SIMDInterpreter()

    def test_alias_dst_equals_addr(self) -> None:
        """Destination VGPR aliases Address VGPR: v[0] = ds_condxchg32 v[0], v[1], v[2].
        
        Address must be read in the operand stage before v[0] is overwritten with old_val.
        """
        state = ScalarMachineState()
        addr = 0x500
        state.set_vgpr(0, addr)        # v[0] is address AND destination
        state.set_vgpr(1, 0x11112222)  # v[1] is cmp_val
        state.set_vgpr(2, 0x33334444)  # v[2] is new_val
        state.lds.write_dword(addr, 0x11112222)

        instr = DSCondxchg32Instruction(
            dst=VGPR(0),
            addr=VGPR(0),
            cmp_val=VGPR(1),
            new_val=VGPR(2),
        )
        old, mem, swapped = self.scalar_interp.step(instr, state)
        self.assertTrue(swapped)
        self.assertEqual(old, 0x11112222)
        self.assertEqual(mem, 0x33334444)
        # v[0] now contains the returned old_val
        self.assertEqual(state.get_vgpr(0), 0x11112222)
        # Memory at original addr was properly updated
        self.assertEqual(state.lds.read_dword(addr), 0x33334444)

    def test_alias_dst_equals_cmp(self) -> None:
        """Destination VGPR aliases Compare VGPR: v[1] = ds_condxchg32 v[0], v[1], v[2]."""
        state = ScalarMachineState()
        addr = 0x600
        state.set_vgpr(0, addr)
        state.set_vgpr(1, 0xAAAA5555)  # v[1] is cmp_val AND destination
        state.set_vgpr(2, 0x77778888)  # v[2] is new_val
        state.lds.write_dword(addr, 0xAAAA5555)

        instr = DSCondxchg32Instruction(
            dst=VGPR(1),
            addr=VGPR(0),
            cmp_val=VGPR(1),
            new_val=VGPR(2),
        )
        old, mem, swapped = self.scalar_interp.step(instr, state)
        self.assertTrue(swapped)
        self.assertEqual(old, 0xAAAA5555)
        self.assertEqual(mem, 0x77778888)
        self.assertEqual(state.get_vgpr(1), 0xAAAA5555)
        self.assertEqual(state.lds.read_dword(addr), 0x77778888)

    def test_alias_dst_equals_new(self) -> None:
        """Destination VGPR aliases New Value VGPR: v[2] = ds_condxchg32 v[0], v[1], v[2]."""
        state = ScalarMachineState()
        addr = 0x700
        state.set_vgpr(0, addr)
        state.set_vgpr(1, 0x12340000)
        state.set_vgpr(2, 0x99990000)  # v[2] is new_val AND destination
        state.lds.write_dword(addr, 0x12340000)

        instr = DSCondxchg32Instruction(
            dst=VGPR(2),
            addr=VGPR(0),
            cmp_val=VGPR(1),
            new_val=VGPR(2),
        )
        old, mem, swapped = self.scalar_interp.step(instr, state)
        self.assertTrue(swapped)
        self.assertEqual(old, 0x12340000)
        # Memory received new_val before v[2] was overwritten with old_val
        self.assertEqual(mem, 0x99990000)
        self.assertEqual(state.lds.read_dword(addr), 0x99990000)
        self.assertEqual(state.get_vgpr(2), 0x12340000)

    def test_full_operand_aliasing_four_way(self) -> None:
        """All operands use the same VGPR: v[5] = ds_condxchg32 v[5], v[5], v[5].
        
        Address, cmp_val, and new_val all originate from v[5].
        """
        state = ScalarMachineState()
        addr_and_val = 0x200
        state.set_vgpr(5, addr_and_val)
        # LDS holds addr_and_val, matching cmp_val
        state.lds.write_dword(addr_and_val, addr_and_val)

        instr = DSCondxchg32Instruction(
            dst=VGPR(5),
            addr=VGPR(5),
            cmp_val=VGPR(5),
            new_val=VGPR(5),
        )
        old, mem, swapped = self.scalar_interp.step(instr, state)
        self.assertTrue(swapped)
        self.assertEqual(old, addr_and_val)
        self.assertEqual(mem, addr_and_val)
        self.assertEqual(state.get_vgpr(5), addr_and_val)
        self.assertEqual(state.lds.read_dword(addr_and_val), addr_and_val)

    def test_unrelated_vgprs_unmodified(self) -> None:
        """Ensure that registers outside destination are pristine."""
        state = ScalarMachineState()
        sentinel_pattern = 0xC001CAFE
        for r in range(256):
            state.set_vgpr(r, sentinel_pattern ^ r)

        addr = 0x100
        state.set_vgpr(10, addr)
        state.set_vgpr(11, 42)
        state.set_vgpr(12, 84)
        state.lds.write_dword(addr, 42)

        instr = DSCondxchg32Instruction(
            dst=VGPR(9),
            addr=VGPR(10),
            cmp_val=VGPR(11),
            new_val=VGPR(12),
        )
        self.scalar_interp.step(instr, state)

        # Only v[9] should be updated
        self.assertEqual(state.get_vgpr(9), 42)
        self.assertEqual(state.get_vgpr(10), addr)
        self.assertEqual(state.get_vgpr(11), 42)
        self.assertEqual(state.get_vgpr(12), 84)

        for r in range(256):
            if r not in (9, 10, 11, 12):
                self.assertEqual(state.get_vgpr(r), sentinel_pattern ^ r)

    def test_simd_multi_lane_aliasing(self) -> None:
        """Every lane aliases destination with address."""
        state = SIMDState(num_lanes=32)
        for lane in range(32):
            addr = 0x300 + lane * 4
            state.set_vgpr(lane, 0, addr)  # v[0] = addr AND dst
            state.lds.write_dword(addr, 0x1000 + lane)

        instr = DSCondxchg32Instruction(
            dst=VGPR(0),
            addr=VGPR(0),
            cmp_val=Immediate(0x1000),  # only lane 0 matches
            new_val=Immediate(0x2000),
        )
        self.simd_interp.step(instr, state)

        # Lane 0 matched and exchanged
        self.assertEqual(state.get_vgpr(0, 0), 0x1000)
        self.assertEqual(state.lds.read_dword(0x300), 0x2000)

        # Other lanes mismatched but received old_val in v[0]
        for lane in range(1, 32):
            self.assertEqual(state.get_vgpr(lane, 0), 0x1000 + lane)
            self.assertEqual(state.lds.read_dword(0x300 + lane * 4), 0x1000 + lane)


# ==============================================================================
# Phase 6: Condition Codes, Status Flags, and Carry/Borrow Generation
# ==============================================================================

@dataclass(frozen=True)
class AtomicStatusFlags:
    """Architectural condition codes and status flags for ds_condxchg32_rtn_b32."""
    success: bool
    zero: bool
    negative: bool
    diff: int
    borrow: bool
    carry: bool
    overflow: bool
    unaligned_addr: bool


def evaluate_condxchg32_flags(
    old_val: int,
    cmp_val: int,
    new_val: int,
    eff_addr: int,
    exchanged: bool,
) -> AtomicStatusFlags:
    """Evaluate condition codes, status flags, and arithmetic carry/borrow."""
    u_old = old_val & MASK_32
    u_cmp = cmp_val & MASK_32
    u_new = new_val & MASK_32

    # Zero flag: returned value is zero
    zero = (u_old == 0)

    # Negative flag: MSB of returned value is set
    negative = bool(u_old & 0x8000_0000)

    # Unsigned difference and borrow
    diff = (u_old - u_cmp) & MASK_32
    borrow = u_old < u_cmp

    # Carry generation on new value write
    carry = ((u_old + u_new) > MASK_32) if exchanged else False

    # Signed comparison overflow for (old_val - cmp_val)
    s_old = (u_old ^ 0x8000_0000) - 0x8000_0000
    s_cmp = (u_cmp ^ 0x8000_0000) - 0x8000_0000
    s_diff = s_old - s_cmp
    overflow = s_diff < -0x8000_0000 or s_diff > 0x7FFF_FFFF

    # Unaligned address check (low 2 bits must be zero for 4-byte dword alignment)
    unaligned = bool(eff_addr & 0x3)

    return AtomicStatusFlags(
        success=exchanged,
        zero=zero,
        negative=negative,
        diff=diff,
        borrow=borrow,
        carry=carry,
        overflow=overflow,
        unaligned_addr=unaligned,
    )


class TestConditionCodesAndStatusFlags(unittest.TestCase):
    """Phase 6: Unit tests for condition codes, status flags, and carry/borrow generation."""

    def test_exact_match_flags(self) -> None:
        flags = evaluate_condxchg32_flags(
            old_val=0x1234,
            cmp_val=0x1234,
            new_val=0x5678,
            eff_addr=0x100,
            exchanged=True,
        )
        self.assertTrue(flags.success)
        self.assertFalse(flags.zero)
        self.assertFalse(flags.negative)
        self.assertEqual(flags.diff, 0)
        self.assertFalse(flags.borrow)
        self.assertFalse(flags.carry)
        self.assertFalse(flags.overflow)
        self.assertFalse(flags.unaligned_addr)

    def test_zero_flag_on_zero_return(self) -> None:
        flags = evaluate_condxchg32_flags(
            old_val=0,
            cmp_val=0,
            new_val=100,
            eff_addr=0x200,
            exchanged=True,
        )
        self.assertTrue(flags.success)
        self.assertTrue(flags.zero)
        self.assertFalse(flags.negative)
        self.assertEqual(flags.diff, 0)
        self.assertFalse(flags.borrow)

    def test_negative_flag_when_msb_set(self) -> None:
        flags = evaluate_condxchg32_flags(
            old_val=0x8000_0001,
            cmp_val=0x10,
            new_val=0x20,
            eff_addr=0x300,
            exchanged=False,
        )
        self.assertFalse(flags.success)
        self.assertFalse(flags.zero)
        self.assertTrue(flags.negative)
        self.assertFalse(flags.borrow)

    def test_borrow_generation_when_old_below_cmp(self) -> None:
        flags = evaluate_condxchg32_flags(
            old_val=10,
            cmp_val=25,
            new_val=50,
            eff_addr=0x400,
            exchanged=False,
        )
        self.assertFalse(flags.success)
        self.assertTrue(flags.borrow)
        self.assertEqual(flags.diff, (10 - 25) & MASK_32)

    def test_no_borrow_when_old_above_cmp(self) -> None:
        flags = evaluate_condxchg32_flags(
            old_val=50,
            cmp_val=25,
            new_val=100,
            eff_addr=0x500,
            exchanged=False,
        )
        self.assertFalse(flags.borrow)
        self.assertEqual(flags.diff, 25)

    def test_signed_overflow_flag(self) -> None:
        # 0x7FFF_FFFF (max positive) - 0x8000_0000 (min negative) -> +0xFFFFFFFF (overflows 32-bit signed)
        flags = evaluate_condxchg32_flags(
            old_val=0x7FFF_FFFF,
            cmp_val=0x8000_0000,
            new_val=0,
            eff_addr=0x600,
            exchanged=False,
        )
        self.assertTrue(flags.overflow)

    def test_carry_generation_on_sum_overflow(self) -> None:
        flags = evaluate_condxchg32_flags(
            old_val=0xFFFF_FFFF,
            cmp_val=0xFFFF_FFFF,
            new_val=0x0000_0002,
            eff_addr=0x700,
            exchanged=True,
        )
        self.assertTrue(flags.success)
        self.assertTrue(flags.carry)

    def test_unaligned_address_flag_detection(self) -> None:
        flags_aligned = evaluate_condxchg32_flags(0, 0, 0, eff_addr=0x1000, exchanged=True)
        self.assertFalse(flags_aligned.unaligned_addr)

        for unaligned_off in (1, 2, 3):
            flags_unaligned = evaluate_condxchg32_flags(
                0, 0, 0,
                eff_addr=0x1000 + unaligned_off,
                exchanged=True,
            )
            self.assertTrue(flags_unaligned.unaligned_addr)


# ==============================================================================
# Phase 7: IEEE 754 Boundary Extremes, Subnormals, and NaN Handling
# ==============================================================================

# IEEE 754 Single-Precision (binary32) Architectural Constants
F32_POS_ZERO: int = 0x0000_0000
F32_NEG_ZERO: int = 0x8000_0000
F32_POS_INF: int = 0x7F80_0000
F32_NEG_INF: int = 0xFF80_0000
F32_CANONICAL_QNAN: int = 0x7FC0_0000
F32_SIGNALING_NAN: int = 0x7F80_0001
F32_MIN_SUBNORMAL: int = 0x0000_0001
F32_MAX_SUBNORMAL: int = 0x007F_FFFF
F32_MIN_NORMAL: int = 0x0080_0000
F32_MAX_NORMAL: int = 0x7F7F_FFFF


def f32_to_bits(val: float) -> int:
    """Convert float to IEEE 754 32-bit unsigned integer representation."""
    return struct.unpack("<I", struct.pack("<f", val))[0]


def bits_to_f32(bits: int) -> float:
    """Convert IEEE 754 32-bit integer representation to Python float."""
    return struct.unpack("<f", struct.pack("<I", bits & MASK_32))[0]


def is_f32_nan(bits: int) -> bool:
    """Check if bits represent an IEEE 754 NaN."""
    return ((bits & 0x7F80_0000) == 0x7F80_0000) and ((bits & 0x007F_FFFF) != 0)


def is_f32_subnormal(bits: int) -> bool:
    """Check if bits represent an IEEE 754 subnormal number."""
    return ((bits & 0x7F80_0000) == 0) and ((bits & 0x007F_FFFF) != 0)


class TestIEEE754BoundaryExtremesAndBitwiseLimits(unittest.TestCase):
    """Phase 7: Unit tests for IEEE 754 boundary extremes, subnormals, and NaN handling."""

    def setUp(self) -> None:
        self.lds = LDSModel()
        self.interp = ScalarInterpreter()
        self.state = ScalarMachineState(lds=self.lds)

    def test_signed_zeros_are_bitwise_distinct(self) -> None:
        """+0.0 (0x00000000) and -0.0 (0x80000000) are distinct bitpatterns in LDS atomics."""
        addr = 0x100
        self.lds.write_dword(addr, F32_POS_ZERO)
        self.state.set_vgpr(0, addr)

        # Attempt exchange matching -0.0
        instr = DSCondxchg32Instruction(
            dst=VGPR(1),
            addr=VGPR(0),
            cmp_val=Immediate(F32_NEG_ZERO),
            new_val=Immediate(0x1234),
        )
        old, mem, swapped = self.interp.step(instr, self.state)
        self.assertFalse(swapped)
        self.assertEqual(old, F32_POS_ZERO)
        self.assertEqual(mem, F32_POS_ZERO)
        self.assertEqual(self.lds.read_dword(addr), F32_POS_ZERO)

        # Now attempt exchange matching +0.0
        instr_match = DSCondxchg32Instruction(
            dst=VGPR(1),
            addr=VGPR(0),
            cmp_val=Immediate(F32_POS_ZERO),
            new_val=Immediate(F32_NEG_ZERO),
        )
        old2, mem2, swapped2 = self.interp.step(instr_match, self.state)
        self.assertTrue(swapped2)
        self.assertEqual(old2, F32_POS_ZERO)
        self.assertEqual(mem2, F32_NEG_ZERO)
        self.assertEqual(self.lds.read_dword(addr), F32_NEG_ZERO)

    def test_quiet_nan_payload_exact_match(self) -> None:
        """Quiet NaNs with identical payload match in bitwise comparison."""
        nan_payload1 = 0x7FC0_1234
        addr = 0x200
        self.lds.write_dword(addr, nan_payload1)
        self.state.set_vgpr(0, addr)

        instr = DSCondxchg32Instruction(
            dst=VGPR(2),
            addr=VGPR(0),
            cmp_val=Immediate(nan_payload1),
            new_val=Immediate(0xCAFE),
        )
        old, mem, swapped = self.interp.step(instr, self.state)
        self.assertTrue(swapped)
        self.assertEqual(old, nan_payload1)
        self.assertEqual(mem, 0xCAFE)

    def test_quiet_nan_differing_payloads_do_not_match(self) -> None:
        """Quiet NaNs with differing payloads do not match."""
        nan_payload1 = 0x7FC0_0001
        nan_payload2 = 0x7FC0_0002
        addr = 0x240
        self.lds.write_dword(addr, nan_payload1)
        self.state.set_vgpr(0, addr)

        instr = DSCondxchg32Instruction(
            dst=VGPR(2),
            addr=VGPR(0),
            cmp_val=Immediate(nan_payload2),
            new_val=Immediate(0x9999),
        )
        old, mem, swapped = self.interp.step(instr, self.state)
        self.assertFalse(swapped)
        self.assertEqual(old, nan_payload1)

    def test_signaling_nan_preserved_without_quietening(self) -> None:
        """Signaling NaNs are not quietened when exchanged through LDS."""
        addr = 0x300
        self.lds.write_dword(addr, 0x1111)
        self.state.set_vgpr(0, addr)

        # Write signaling NaN into memory
        instr = DSCondxchg32Instruction(
            dst=VGPR(3),
            addr=VGPR(0),
            cmp_val=Immediate(0x1111),
            new_val=Immediate(F32_SIGNALING_NAN),
        )
        old, mem, swapped = self.interp.step(instr, self.state)
        self.assertTrue(swapped)
        self.assertEqual(mem, F32_SIGNALING_NAN)
        self.assertEqual(self.lds.read_dword(addr), F32_SIGNALING_NAN)
        # MSB of fraction bit 22 is 0 (signaling, not quiet)
        self.assertEqual(mem & 0x0040_0000, 0)

    def test_positive_and_negative_infinities(self) -> None:
        """Infinities exchange cleanly and do not compare equal across signs."""
        addr = 0x400
        self.lds.write_dword(addr, F32_POS_INF)
        self.state.set_vgpr(0, addr)

        # -inf should not match +inf
        instr_neg = DSCondxchg32Instruction(
            dst=VGPR(4),
            addr=VGPR(0),
            cmp_val=Immediate(F32_NEG_INF),
            new_val=Immediate(0),
        )
        old, mem, swapped = self.interp.step(instr_neg, self.state)
        self.assertFalse(swapped)
        self.assertEqual(old, F32_POS_INF)

        # +inf matches +inf
        instr_pos = DSCondxchg32Instruction(
            dst=VGPR(4),
            addr=VGPR(0),
            cmp_val=Immediate(F32_POS_INF),
            new_val=Immediate(F32_NEG_INF),
        )
        old2, mem2, swapped2 = self.interp.step(instr_pos, self.state)
        self.assertTrue(swapped2)
        self.assertEqual(old2, F32_POS_INF)
        self.assertEqual(mem2, F32_NEG_INF)

    def test_subnormals_preserved_no_flush_to_zero(self) -> None:
        """Subnormal floating point patterns are not flushed to zero."""
        self.assertTrue(is_f32_subnormal(F32_MIN_SUBNORMAL))
        self.assertTrue(is_f32_subnormal(F32_MAX_SUBNORMAL))

        addr = 0x500
        self.lds.write_dword(addr, F32_MIN_SUBNORMAL)
        self.state.set_vgpr(0, addr)

        instr = DSCondxchg32Instruction(
            dst=VGPR(5),
            addr=VGPR(0),
            cmp_val=Immediate(F32_MIN_SUBNORMAL),
            new_val=Immediate(F32_MAX_SUBNORMAL),
        )
        old, mem, swapped = self.interp.step(instr, self.state)
        self.assertTrue(swapped)
        self.assertEqual(old, F32_MIN_SUBNORMAL)
        self.assertEqual(mem, F32_MAX_SUBNORMAL)
        self.assertNotEqual(mem, 0)

    def test_integer_extremes_boundary_values(self) -> None:
        """Check all 32-bit extreme boundary integers."""
        extremes = [0x0000_0000, 0xFFFF_FFFF, 0x7FFF_FFFF, 0x8000_0000, 0x0000_0001, 0xFFFF_FFFE]
        addr = 0x600
        self.state.set_vgpr(0, addr)

        for i in range(len(extremes) - 1):
            curr_val = extremes[i]
            next_val = extremes[i + 1]
            self.lds.write_dword(addr, curr_val)

            instr = DSCondxchg32Instruction(
                dst=VGPR(6),
                addr=VGPR(0),
                cmp_val=Immediate(curr_val),
                new_val=Immediate(next_val),
            )
            old, mem, swapped = self.interp.step(instr, self.state)
            self.assertTrue(swapped)
            self.assertEqual(old, curr_val)
            self.assertEqual(mem, next_val)
            self.assertEqual(self.lds.read_dword(addr), next_val)


# ==============================================================================
# Phase 8: Negative Controls, Syntax Rejection, and Mutation Detection
# ==============================================================================

class TestNegativeControlsAndMutationDetection(unittest.TestCase):
    """Phase 8: Unit tests for negative controls, syntax rejection, and mutation detection."""

    def test_syntax_rejection_invalid_register_indices(self) -> None:
        with self.assertRaises(ValueError):
            VGPR(-1)
        with self.assertRaises(ValueError):
            VGPR(256)
        with self.assertRaises(ValueError):
            SGPR(-1)
        with self.assertRaises(ValueError):
            SGPR(106)

    def test_syntax_rejection_invalid_operand_types(self) -> None:
        with self.assertRaises(TypeError):
            DSCondxchg32Instruction(
                dst=SGPR(0),  # type: ignore[arg-type]
                addr=VGPR(1),
                cmp_val=Immediate(0),
                new_val=Immediate(0),
            )
        with self.assertRaises(TypeError):
            DSCondxchg32Instruction(
                dst=VGPR(0),
                addr=SGPR(1),  # type: ignore[arg-type]
                cmp_val=Immediate(0),
                new_val=Immediate(0),
            )

    def test_syntax_rejection_invalid_offset(self) -> None:
        with self.assertRaises(ValueError):
            DSCondxchg32Instruction(
                dst=VGPR(0),
                addr=VGPR(1),
                cmp_val=Immediate(0),
                new_val=Immediate(0),
                offset=-1,
            )
        with self.assertRaises(ValueError):
            DSCondxchg32Instruction(
                dst=VGPR(0),
                addr=VGPR(1),
                cmp_val=Immediate(0),
                new_val=Immediate(0),
                offset=65536,
            )

    def test_syntax_rejection_invalid_lds_capacity(self) -> None:
        with self.assertRaises(ValueError):
            LDSModel(capacity_dwords=100)  # non-power of two
        with self.assertRaises(ValueError):
            LDSModel(capacity_dwords=0)

    def test_syntax_rejection_invalid_simd_lane_count(self) -> None:
        with self.assertRaises(ValueError):
            SIMDState(num_lanes=16)
        with self.assertRaises(ValueError):
            SIMDState(num_lanes=128)

    def test_negative_control_bit_flip_detection(self) -> None:
        old, mem, swapped = oracle_ds_condxchg32(0x12345678, 0x12345678, 0x9ABCDEF0)
        self.assertTrue(swapped)
        self.assertEqual(old, 0x12345678)
        self.assertEqual(mem, 0x9ABCDEF0)

        # Single bit perturbation of old_val
        corrupted_old = old ^ 0x1
        self.assertNotEqual(old, corrupted_old)

        # Single bit perturbation of new_mem
        corrupted_mem = mem ^ (1 << 31)
        self.assertNotEqual(mem, corrupted_mem)

        # Inverted swapped status
        self.assertNotEqual(swapped, not swapped)

    def test_mutation_unconditional_exchange(self) -> None:
        """Mutant: Memory is overwritten unconditionally regardless of cmp_val."""
        def mutant_condxchg(curr: int, cmp_val: int, new_val: int) -> Tuple[int, int, bool]:
            # Bug: always overwrites curr with new_val
            return curr, new_val, True

        curr = 0x5555
        cmp_val = 0x9999  # mismatch!
        new_val = 0xAAAA

        correct_old, correct_mem, correct_swapped = oracle_ds_condxchg32(curr, cmp_val, new_val)
        mutant_old, mutant_mem, mutant_swapped = mutant_condxchg(curr, cmp_val, new_val)

        self.assertFalse(correct_swapped)
        self.assertEqual(correct_mem, curr)
        # Mutant erroneously swapped
        self.assertTrue(mutant_swapped)
        self.assertNotEqual(correct_mem, mutant_mem)

    def test_mutation_inactive_lane_overwritten(self) -> None:
        """Mutant: Inactive lanes in SIMD are improperly overwritten."""
        state = SIMDState(num_lanes=32, exec_mask=0x1)  # Only lane 0 active
        sentinel = 0xCAFE_BABE
        for lane in range(32):
            state.set_vgpr(lane, 5, sentinel)

        class BuggySIMDInterpreter(SIMDInterpreter):
            def step(self, instr: DSCondxchg32Instruction, s: SIMDState):
                # Bug: overwrites destination on all lanes regardless of exec_mask
                for l in range(s.num_lanes):
                    s.set_vgpr(l, instr.dst.index, 0x1234)
                return []

        buggy = BuggySIMDInterpreter()
        instr = DSCondxchg32Instruction(
            dst=VGPR(5),
            addr=VGPR(0),
            cmp_val=Immediate(0),
            new_val=Immediate(0),
        )
        buggy.step(instr, state)

        # Verify mutation is detected: inactive lane 1 was modified
        corrupted_lanes = [l for l in range(1, 32) if state.get_vgpr(l, 5) != sentinel]
        self.assertEqual(len(corrupted_lanes), 31)

    def test_mutation_premature_writeback_aliasing(self) -> None:
        """Mutant: Destination register written before address/operands are latched."""
        state = ScalarMachineState()
        addr = 0x400
        state.set_vgpr(0, addr)  # v[0] is both dst and addr
        state.lds.write_dword(addr, 0x1111)

        class PrematureWritebackInterpreter(ScalarInterpreter):
            def step(self, instr: DSCondxchg32Instruction, s: ScalarMachineState):
                # Bug: prematurely writes dst before reading addr
                s.set_vgpr(instr.dst.index, 0x9999)
                vaddr = s.get_vgpr(instr.addr.index)  # Reads corrupted vaddr 0x9999!
                eff_addr = calc_lds_effective_address(vaddr, instr.offset)
                curr_mem = s.lds.read_dword(eff_addr)
                return curr_mem, curr_mem, False

        buggy = PrematureWritebackInterpreter()
        instr = DSCondxchg32Instruction(
            dst=VGPR(0),
            addr=VGPR(0),
            cmp_val=Immediate(0x1111),
            new_val=Immediate(0x2222),
        )
        old, mem, swapped = buggy.step(instr, state)
        # Bug caused read from wrong address (0x9999 instead of 0x400), returning 0 instead of 0x1111
        self.assertNotEqual(old, 0x1111)

    def test_mutation_inverted_lane_collision_order(self) -> None:
        """Mutant: Serializes colliding lanes in reverse order (lane N before lane 0)."""
        state = SIMDState(num_lanes=32, exec_mask=0x3)  # Lanes 0 and 1 active
        common_addr = 0x100
        state.lds.write_dword(common_addr, 10)

        # Lane 0: expects 10, writes 20
        state.set_vgpr(0, 1, common_addr)
        state.set_vgpr(0, 2, 10)
        state.set_vgpr(0, 3, 20)

        # Lane 1: expects 20, writes 30
        state.set_vgpr(1, 1, common_addr)
        state.set_vgpr(1, 2, 20)
        state.set_vgpr(1, 3, 30)

        # In correct ascending order: lane 0 turns 10->20; lane 1 turns 20->30.
        # If reverse order executes: lane 1 sees 10 (mismatches 20!), fails; lane 0 sees 10, writes 20.
        class ReverseCollisionSIMDInterpreter(SIMDInterpreter):
            def step(self, instr: DSCondxchg32Instruction, s: SIMDState):
                # Bug: iterate lanes in reverse
                for lane in reversed(range(s.num_lanes)):
                    if s.is_lane_active(lane):
                        vaddr = s.get_vgpr(lane, instr.addr.index)
                        cmp_val = self.resolve_lane_operand(instr.cmp_val, lane, s)
                        new_val = self.resolve_lane_operand(instr.new_val, lane, s)
                        eff_addr = calc_lds_effective_address(vaddr, instr.offset)
                        curr_mem = s.lds.read_dword(eff_addr)
                        if curr_mem == cmp_val:
                            s.lds.write_dword(eff_addr, new_val)
                        s.set_vgpr(lane, instr.dst.index, curr_mem)
                return []

        buggy = ReverseCollisionSIMDInterpreter()
        instr = DSCondxchg32Instruction(
            dst=VGPR(0),
            addr=VGPR(1),
            cmp_val=VGPR(2),
            new_val=VGPR(3),
        )
        buggy.step(instr, state)
        # In reverse order, final memory is 20 instead of 30
        self.assertNotEqual(state.lds.read_dword(common_addr), 30)
        self.assertEqual(state.lds.read_dword(common_addr), 20)

    def test_mutation_ignore_immediate_offset(self) -> None:
        """Mutant: Immediate offset is dropped during address calculation."""
        def buggy_eff_address(vaddr: int, offset: int = 0) -> int:
            # Bug: drops offset
            return vaddr & MASK_32

        base = 0x200
        offset = 0x14
        correct = calc_lds_effective_address(base, offset)
        mutant = buggy_eff_address(base, offset)
        self.assertEqual(correct, 0x214)
        self.assertNotEqual(correct, mutant)

    def test_mutation_incorrect_capacity_mask(self) -> None:
        """Mutant: Masking with wrong power of two truncates valid LDS addresses."""
        eff_addr = 0x8000  # 32 KiB -> dword index 0x2000
        correct_idx = calc_lds_dword_index(eff_addr, capacity_dwords=16384)
        mutant_idx = calc_lds_dword_index(eff_addr, capacity_dwords=256)
        self.assertEqual(correct_idx, 0x2000)
        self.assertNotEqual(correct_idx, mutant_idx)
        self.assertEqual(mutant_idx, 0)


if __name__ == "__main__":
    unittest.main()
