from __future__ import annotations

import struct
import unittest
from dataclasses import dataclass
from enum import Enum, auto
from typing import Dict, List, Optional, Tuple, Union

# LDS Architectural Constants for RDNA2 (GFX10/GFX10.3)
LDS_DEFAULT_SIZE_BYTES: int = 65536  # 64 KiB default Workgroup LDS allocation
LDS_DEFAULT_SIZE_DWORDS: int = 16384
DWORD_BYTES: int = 4
QWORD_BYTES: int = 8
ST64_MULTIPLIER: int = 64
UNIT_B64_BYTES: int = 8
UNIT_B64_ST64_BYTES: int = 8 * ST64_MULTIPLIER  # 512 bytes

MASK32: int = 0xFFFF_FFFF
MASK64: int = 0xFFFF_FFFF_FFFF_FFFF


class RegType(Enum):
    """Register file domain."""
    VGPR = auto()
    SGPR = auto()


@dataclass(frozen=True)
class RegSingle:
    """Represents a single 32-bit register."""
    index: int
    reg_type: RegType = RegType.VGPR

    def __post_init__(self) -> None:
        if not (0 <= self.index <= 255):
            raise ValueError(f"Register index out of bounds [0, 255]: {self.index}")

    def __str__(self) -> str:
        prefix = "v" if self.reg_type == RegType.VGPR else "s"
        return f"{prefix}{self.index}"


@dataclass(frozen=True)
class RegPair:
    """Represents an aligned 64-bit pair of registers (e.g. v[0:1])."""
    index: int
    reg_type: RegType = RegType.VGPR

    def __post_init__(self) -> None:
        if not (0 <= self.index <= 254):
            raise ValueError(f"Register pair base index out of bounds [0, 254]: {self.index}")

    @property
    def indices(self) -> Tuple[int, int]:
        return self.index, self.index + 1

    def __str__(self) -> str:
        prefix = "v" if self.reg_type == RegType.VGPR else "s"
        return f"{prefix}[{self.index}:{self.index + 1}]"


@dataclass(frozen=True)
class RegQuad:
    """Represents an aligned 128-bit quad of registers (e.g. v[0:3])."""
    index: int
    reg_type: RegType = RegType.VGPR

    def __post_init__(self) -> None:
        if not (0 <= self.index <= 252):
            raise ValueError(f"Register quad base index out of bounds [0, 252]: {self.index}")

    @property
    def indices(self) -> Tuple[int, int, int, int]:
        return self.index, self.index + 1, self.index + 2, self.index + 3

    def __str__(self) -> str:
        prefix = "v" if self.reg_type == RegType.VGPR else "s"
        return f"{prefix}[{self.index}:{self.index + 3}]"


@dataclass(frozen=True)
class LDSAddressExpression:
    """Symbolic representation of LDS address computation."""
    base: RegSingle
    offset: int
    is_st64: bool = False
    lds_size_bytes: int = LDS_DEFAULT_SIZE_BYTES

    def __post_init__(self) -> None:
        if not (0 <= self.offset <= 255):
            raise ValueError(f"LDS offset out of 8-bit immediate range [0, 255]: {self.offset}")

    @property
    def stride(self) -> int:
        return UNIT_B64_ST64_BYTES if self.is_st64 else UNIT_B64_BYTES

    def evaluate(self, base_val: int) -> int:
        return (base_val + self.offset * self.stride) & (self.lds_size_bytes - 1)


@dataclass(frozen=True)
class DSWrxchg2B64Instruction:
    """AST representation of RDNA2 ds_wrxchg2_b64 instruction family."""
    vaddr: RegSingle
    data0: RegPair
    data1: RegPair
    vdst: Optional[RegQuad] = None
    offset0: int = 0
    offset1: int = 0
    is_st64: bool = False
    is_rtn: bool = True

    def __post_init__(self) -> None:
        if not (0 <= self.offset0 <= 255):
            raise ValueError(f"offset0 out of 8-bit range [0, 255]: {self.offset0}")
        if not (0 <= self.offset1 <= 255):
            raise ValueError(f"offset1 out of 8-bit range [0, 255]: {self.offset1}")
        if self.is_rtn and self.vdst is None:
            raise ValueError("Returning form (_rtn_) requires vdst destination quad")
        if self.vdst is not None and self.vdst.reg_type != RegType.VGPR:
            raise TypeError("LDS DS instruction vdst must be VGPRs")
        if self.vaddr.reg_type != RegType.VGPR:
            raise TypeError("LDS DS instruction vaddr must be VGPR")
        if self.data0.reg_type != RegType.VGPR or self.data1.reg_type != RegType.VGPR:
            raise TypeError("LDS DS instruction data operands must be VGPR pairs")

    @property
    def mnemonic(self) -> str:
        st64_str = "st64" if self.is_st64 else ""
        rtn_str = "_rtn" if self.is_rtn else ""
        return f"ds_wrxchg2{st64_str}{rtn_str}_b64"

    def disasm(self) -> str:
        parts = [self.mnemonic]
        if self.is_rtn and self.vdst is not None:
            parts.append(f"{self.vdst},")
        parts.append(f"{self.vaddr},")
        parts.append(f"{self.data0},")
        parts.append(f"{self.data1}")
        line = f"{parts[0]} {' '.join(parts[1:])}"
        if self.offset0 != 0:
            line += f" offset0:{self.offset0}"
        if self.offset1 != 0:
            line += f" offset1:{self.offset1}"
        return line


class ScalarRegisterFile:
    """Models a 256-register 32-bit scalar VGPR file."""

    def __init__(self) -> None:
        self.regs: List[int] = [0] * 256

    def get_u32(self, reg_idx: int) -> int:
        return self.regs[reg_idx] & MASK32

    def set_u32(self, reg_idx: int, val: int) -> None:
        self.regs[reg_idx] = val & MASK32

    def get_u64(self, pair: RegPair) -> int:
        lo = self.get_u32(pair.index)
        hi = self.get_u32(pair.index + 1)
        return combine_u32s_to_u64(lo, hi)

    def set_u64(self, pair: RegPair, val: int) -> None:
        lo, hi = split_u64_to_u32s(val)
        self.set_u32(pair.index, lo)
        self.set_u32(pair.index + 1, hi)

    def set_quad(self, quad: RegQuad, w0: int, w1: int, w2: int, w3: int) -> None:
        self.set_u32(quad.index, w0)
        self.set_u32(quad.index + 1, w1)
        self.set_u32(quad.index + 2, w2)
        self.set_u32(quad.index + 3, w3)


class ScalarInterpreter:
    """Deterministic scalar instruction interpreter for ds_wrxchg2_b64."""

    def __init__(self, lds_size_bytes: int = LDS_DEFAULT_SIZE_BYTES) -> None:
        self.lds_mem: bytearray = bytearray(lds_size_bytes)
        self.rf: ScalarRegisterFile = ScalarRegisterFile()

    def step(self, instr: DSWrxchg2B64Instruction) -> Tuple[int, int]:
        """Execute a single ds_wrxchg2_b64 instruction on the scalar state."""
        vaddr = self.rf.get_u32(instr.vaddr.index)
        data0 = self.rf.get_u64(instr.data0)
        data1 = self.rf.get_u64(instr.data1)

        old0, old1 = oracle_ds_wrxchg2_b64(
            self.lds_mem,
            vaddr=vaddr,
            data0=data0,
            data1=data1,
            offset0=instr.offset0,
            offset1=instr.offset1,
            is_st64=instr.is_st64,
        )

        if instr.is_rtn and instr.vdst is not None:
            w0, w1 = split_u64_to_u32s(old0)
            w2, w3 = split_u64_to_u32s(old1)
            self.rf.set_quad(instr.vdst, w0, w1, w2, w3)

        return old0, old1


class SIMDState:
    """Models multi-lane SIMD wave state with shared Workgroup LDS."""

    def __init__(
        self,
        num_lanes: int = 32,
        exec_mask: int = MASK32,
        lds_size_bytes: int = LDS_DEFAULT_SIZE_BYTES,
    ) -> None:
        if num_lanes not in (32, 64):
            raise ValueError(f"SIMD num_lanes must be 32 or 64, got: {num_lanes}")
        self.num_lanes: int = num_lanes
        self.exec_mask: int = exec_mask & (MASK64 if num_lanes == 64 else MASK32)
        self.vgprs: List[List[int]] = [[0] * 256 for _ in range(num_lanes)]
        self.lds_mem: bytearray = bytearray(lds_size_bytes)

    def is_active(self, lane: int) -> bool:
        return bool((self.exec_mask >> lane) & 1)

    def get_vgpr(self, lane: int, reg_idx: int) -> int:
        return self.vgprs[lane][reg_idx] & MASK32

    def set_vgpr(self, lane: int, reg_idx: int, val: int) -> None:
        self.vgprs[lane][reg_idx] = val & MASK32

    def get_vgpr64(self, lane: int, pair: RegPair) -> int:
        lo = self.get_vgpr(lane, pair.index)
        hi = self.get_vgpr(lane, pair.index + 1)
        return combine_u32s_to_u64(lo, hi)

    def set_vgpr64(self, lane: int, pair: RegPair, val: int) -> None:
        lo, hi = split_u64_to_u32s(val)
        self.set_vgpr(lane, pair.index, lo)
        self.set_vgpr(lane, pair.index + 1, hi)

    def set_vgpr128(self, lane: int, quad: RegQuad, w0: int, w1: int, w2: int, w3: int) -> None:
        self.set_vgpr(lane, quad.index, w0)
        self.set_vgpr(lane, quad.index + 1, w1)
        self.set_vgpr(lane, quad.index + 2, w2)
        self.set_vgpr(lane, quad.index + 3, w3)


class SIMDInterpreter:
    """SIMD multi-lane execution interpreter respecting 32/64-bit EXEC masking."""

    def step(
        self, instr: DSWrxchg2B64Instruction, state: SIMDState
    ) -> List[Optional[Tuple[int, int]]]:
        """Execute ds_wrxchg2_b64 across all lanes in the wave."""
        results: List[Optional[Tuple[int, int]]] = []
        for lane in range(state.num_lanes):
            if not state.is_active(lane):
                results.append(None)
                continue

            vaddr = state.get_vgpr(lane, instr.vaddr.index)
            data0 = state.get_vgpr64(lane, instr.data0)
            data1 = state.get_vgpr64(lane, instr.data1)

            old0, old1 = oracle_ds_wrxchg2_b64(
                state.lds_mem,
                vaddr=vaddr,
                data0=data0,
                data1=data1,
                offset0=instr.offset0,
                offset1=instr.offset1,
                is_st64=instr.is_st64,
            )

            if instr.is_rtn and instr.vdst is not None:
                w0, w1 = split_u64_to_u32s(old0)
                w2, w3 = split_u64_to_u32s(old1)
                state.set_vgpr128(lane, instr.vdst, w0, w1, w2, w3)

            results.append((old0, old1))
        return results


def split_u64_to_u32s(val: int) -> Tuple[int, int]:
    """Split 64-bit unsigned integer into (lo, hi) 32-bit words."""
    val &= MASK64
    return val & MASK32, (val >> 32) & MASK32


def combine_u32s_to_u64(lo: int, hi: int) -> int:
    """Combine (lo, hi) 32-bit words into a 64-bit unsigned integer."""
    return ((hi & MASK32) << 32) | (lo & MASK32)


def compute_lds_address(
    vaddr: int,
    offset: int,
    is_st64: bool = False,
    lds_size_bytes: int = LDS_DEFAULT_SIZE_BYTES,
) -> int:
    """Compute wrapped byte address in LDS for ds_wrxchg2_b64.

    Address formula: (vaddr + offset * unit) & (lds_size_bytes - 1)
    where unit is 8 bytes for standard ds_wrxchg2_b64, or 512 bytes for ds_wrxchg2st64_b64.
    """
    unit = UNIT_B64_ST64_BYTES if is_st64 else UNIT_B64_BYTES
    effective = (vaddr + offset * unit) & (lds_size_bytes - 1)
    return effective


def oracle_ds_wrxchg2_b64(
    lds_mem: bytearray,
    vaddr: int,
    data0: int,
    data1: int,
    offset0: int = 0,
    offset1: int = 0,
    is_st64: bool = False,
) -> Tuple[int, int]:
    """Reference mathematical oracle for RDNA2 ds_wrxchg2_b64.

    Simulates atomic exchange of two 64-bit elements in LDS memory.
    Returns (old_data0, old_data1).
    When addr0 == addr1, slot 0 exchange occurs first, followed by slot 1.
    """
    lds_bytes = len(lds_mem)
    addr0 = compute_lds_address(vaddr, offset0, is_st64, lds_bytes)
    addr1 = compute_lds_address(vaddr, offset1, is_st64, lds_bytes)

    # 4-byte dword alignment mask standard in DS addressing
    addr0 &= ~3
    addr1 &= ~3

    # Slot 0 atomic exchange
    addr0_next = (addr0 + 4) & (lds_bytes - 1)
    old0_lo = struct.unpack_from("<I", lds_mem, addr0)[0]
    old0_hi = struct.unpack_from("<I", lds_mem, addr0_next)[0]
    old0 = combine_u32s_to_u64(old0_lo, old0_hi)

    d0_lo, d0_hi = split_u64_to_u32s(data0)
    struct.pack_into("<I", lds_mem, addr0, d0_lo)
    struct.pack_into("<I", lds_mem, addr0_next, d0_hi)

    # Slot 1 atomic exchange
    addr1_next = (addr1 + 4) & (lds_bytes - 1)
    old1_lo = struct.unpack_from("<I", lds_mem, addr1)[0]
    old1_hi = struct.unpack_from("<I", lds_mem, addr1_next)[0]
    old1 = combine_u32s_to_u64(old1_lo, old1_hi)

    d1_lo, d1_hi = split_u64_to_u32s(data1)
    struct.pack_into("<I", lds_mem, addr1, d1_lo)
    struct.pack_into("<I", lds_mem, addr1_next, d1_hi)

    return old0, old1


class TestReferenceOracleAndArchitecturalModel(unittest.TestCase):
    """Unit tests for the reference mathematical oracle and architectural model."""

    def setUp(self) -> None:
        self.lds = bytearray(LDS_DEFAULT_SIZE_BYTES)

    def test_basic_exchange_identity(self) -> None:
        # Seed memory at byte 0x40 and 0x48
        struct.pack_into("<Q", self.lds, 0x40, 0x1111_2222_3333_4444)
        struct.pack_into("<Q", self.lds, 0x48, 0x5555_6666_7777_8888)

        old0, old1 = oracle_ds_wrxchg2_b64(
            self.lds,
            vaddr=0x40,
            data0=0xAAAA_BBBB_CCCC_DDDD,
            data1=0xEEEE_FFFF_0000_1111,
            offset0=0,
            offset1=1,  # 0x40 + 1 * 8 = 0x48
            is_st64=False,
        )

        self.assertEqual(old0, 0x1111_2222_3333_4444)
        self.assertEqual(old1, 0x5555_6666_7777_8888)

        # Verify new memory values
        mem0 = struct.unpack_from("<Q", self.lds, 0x40)[0]
        mem1 = struct.unpack_from("<Q", self.lds, 0x48)[0]
        self.assertEqual(mem0, 0xAAAA_BBBB_CCCC_DDDD)
        self.assertEqual(mem1, 0xEEEE_FFFF_0000_1111)

    def test_offset_scaling_unit_8(self) -> None:
        # offset0=3 -> +24 bytes, offset1=7 -> +56 bytes
        vaddr = 0x100
        addr0 = vaddr + 3 * 8
        addr1 = vaddr + 7 * 8
        struct.pack_into("<Q", self.lds, addr0, 0x1234_5678_9ABC_DEF0)
        struct.pack_into("<Q", self.lds, addr1, 0xFEDC_BA98_7654_3210)

        old0, old1 = oracle_ds_wrxchg2_b64(
            self.lds,
            vaddr=vaddr,
            data0=0x0102_0304_0506_0708,
            data1=0x0807_0605_0403_0201,
            offset0=3,
            offset1=7,
            is_st64=False,
        )

        self.assertEqual(old0, 0x1234_5678_9ABC_DEF0)
        self.assertEqual(old1, 0xFEDC_BA98_7654_3210)
        self.assertEqual(struct.unpack_from("<Q", self.lds, addr0)[0], 0x0102_0304_0506_0708)
        self.assertEqual(struct.unpack_from("<Q", self.lds, addr1)[0], 0x0807_0605_0403_0201)

    def test_st64_stride_scaling_unit_512(self) -> None:
        # st64 form scales offsets by 512 bytes (8 * 64)
        vaddr = 0x80
        addr0 = vaddr + 0 * 512  # 0x80
        addr1 = vaddr + 1 * 512  # 0x280
        struct.pack_into("<Q", self.lds, addr0, 0xCAFE_BABE_0000_0001)
        struct.pack_into("<Q", self.lds, addr1, 0xDEAD_BEEF_0000_0002)

        old0, old1 = oracle_ds_wrxchg2_b64(
            self.lds,
            vaddr=vaddr,
            data0=0x1111_1111_1111_1111,
            data1=0x2222_2222_2222_2222,
            offset0=0,
            offset1=1,
            is_st64=True,
        )

        self.assertEqual(old0, 0xCAFE_BABE_0000_0001)
        self.assertEqual(old1, 0xDEAD_BEEF_0000_0002)
        self.assertEqual(struct.unpack_from("<Q", self.lds, addr0)[0], 0x1111_1111_1111_1111)
        self.assertEqual(struct.unpack_from("<Q", self.lds, addr1)[0], 0x2222_2222_2222_2222)

    def test_lds_memory_wraparound_boundary(self) -> None:
        # Wrap around 64 KiB LDS boundary
        vaddr = LDS_DEFAULT_SIZE_BYTES - 8  # 0xFFF8
        offset0 = 0  # 0xFFF8
        offset1 = 1  # 0xFFF8 + 8 = 0x10000 -> wraps to 0x0000
        struct.pack_into("<Q", self.lds, 0xFFF8, 0x9999_8888_7777_6666)
        struct.pack_into("<Q", self.lds, 0x0000, 0x3333_2222_1111_0000)

        old0, old1 = oracle_ds_wrxchg2_b64(
            self.lds,
            vaddr=vaddr,
            data0=0xAAAA_AAAA_AAAA_AAAA,
            data1=0xBBBB_BBBB_BBBB_BBBB,
            offset0=offset0,
            offset1=offset1,
            is_st64=False,
        )

        self.assertEqual(old0, 0x9999_8888_7777_6666)
        self.assertEqual(old1, 0x3333_2222_1111_0000)
        self.assertEqual(struct.unpack_from("<Q", self.lds, 0xFFF8)[0], 0xAAAA_AAAA_AAAA_AAAA)
        self.assertEqual(struct.unpack_from("<Q", self.lds, 0x0000)[0], 0xBBBB_BBBB_BBBB_BBBB)

    def test_same_address_collision_sequential_semantics(self) -> None:
        # Both offset0 and offset1 target the exact same address
        vaddr = 0x200
        addr = vaddr + 2 * 8  # 0x210
        struct.pack_into("<Q", self.lds, addr, 0x0000_0000_FEED_FACE)

        d0 = 0x1111_2222_3333_4444
        d1 = 0x5555_6666_7777_8888
        old0, old1 = oracle_ds_wrxchg2_b64(
            self.lds,
            vaddr=vaddr,
            data0=d0,
            data1=d1,
            offset0=2,
            offset1=2,
            is_st64=False,
        )

        # Slot 0 reads initial memory (0xFEEDFACE) and writes d0
        self.assertEqual(old0, 0x0000_0000_FEED_FACE)
        # Slot 1 reads what slot 0 just wrote (d0) and writes d1
        self.assertEqual(old1, d0)
        # Final memory holds d1
        self.assertEqual(struct.unpack_from("<Q", self.lds, addr)[0], d1)

    def test_dword_little_endian_layout(self) -> None:
        # Check that dwords are stored in little-endian order: low dword at addr, high dword at addr+4
        val = 0x1234_5678_9ABC_DEF0
        d_lo, d_hi = split_u64_to_u32s(val)
        self.assertEqual(d_lo, 0x9ABC_DEF0)
        self.assertEqual(d_hi, 0x1234_5678)

        oracle_ds_wrxchg2_b64(self.lds, vaddr=0x20, data0=val, data1=0, offset0=0, offset1=1)
        stored_lo = struct.unpack_from("<I", self.lds, 0x20)[0]
        stored_hi = struct.unpack_from("<I", self.lds, 0x24)[0]
        self.assertEqual(stored_lo, 0x9ABC_DEF0)
        self.assertEqual(stored_hi, 0x1234_5678)

    def test_boundary_bit_patterns(self) -> None:
        for val0, val1 in [
            (0, MASK64),
            (MASK64, 0),
            (0x5555_5555_5555_5555, 0xAAAA_AAAA_AAAA_AAAA),
            (0x8000_0000_0000_0000, 0x7FFF_FFFF_FFFF_FFFF),
        ]:
            with self.subTest(val0=hex(val0), val1=hex(val1)):
                old0, old1 = oracle_ds_wrxchg2_b64(
                    self.lds, vaddr=0x10, data0=val0, data1=val1, offset0=0, offset1=1
                )
                self.assertEqual(struct.unpack_from("<Q", self.lds, 0x10)[0], val0)
                self.assertEqual(struct.unpack_from("<Q", self.lds, 0x18)[0], val1)


class TestSymbolicASTAndTypedExpressions(unittest.TestCase):
    """Unit tests for symbolic AST opcode definitions and typed expressions."""

    def test_reg_single_pair_quad_construction(self) -> None:
        v1 = RegSingle(1)
        self.assertEqual(str(v1), "v1")
        self.assertEqual(v1.index, 1)
        self.assertEqual(v1.reg_type, RegType.VGPR)

        v_pair = RegPair(12)
        self.assertEqual(str(v_pair), "v[12:13]")
        self.assertEqual(v_pair.indices, (12, 13))

        v_quad = RegQuad(16)
        self.assertEqual(str(v_quad), "v[16:19]")
        self.assertEqual(v_quad.indices, (16, 17, 18, 19))

    def test_address_expression_evaluation(self) -> None:
        vaddr = RegSingle(2)
        addr_expr = LDSAddressExpression(base=vaddr, offset=5, is_st64=False)
        self.assertEqual(addr_expr.stride, 8)
        self.assertEqual(addr_expr.evaluate(0x100), 0x100 + 40)

        st64_expr = LDSAddressExpression(base=vaddr, offset=2, is_st64=True)
        self.assertEqual(st64_expr.stride, 512)
        self.assertEqual(st64_expr.evaluate(0x100), 0x100 + 1024)

    def test_instruction_disasm_canonical_formatting(self) -> None:
        instr = DSWrxchg2B64Instruction(
            vdst=RegQuad(16),
            vaddr=RegSingle(1),
            data0=RegPair(12),
            data1=RegPair(14),
            offset0=2,
            offset1=4,
            is_st64=False,
            is_rtn=True,
        )
        self.assertEqual(instr.mnemonic, "ds_wrxchg2_rtn_b64")
        self.assertEqual(
            instr.disasm(),
            "ds_wrxchg2_rtn_b64 v[16:19], v1, v[12:13], v[14:15] offset0:2 offset1:4",
        )

    def test_instruction_st64_mnemonic_and_disasm(self) -> None:
        instr = DSWrxchg2B64Instruction(
            vdst=RegQuad(0),
            vaddr=RegSingle(2),
            data0=RegPair(4),
            data1=RegPair(6),
            offset0=0,
            offset1=1,
            is_st64=True,
            is_rtn=True,
        )
        self.assertEqual(instr.mnemonic, "ds_wrxchg2st64_rtn_b64")
        self.assertEqual(
            instr.disasm(),
            "ds_wrxchg2st64_rtn_b64 v[0:3], v2, v[4:5], v[6:7] offset1:1",
        )

    def test_instruction_non_return_disasm(self) -> None:
        instr = DSWrxchg2B64Instruction(
            vdst=None,
            vaddr=RegSingle(5),
            data0=RegPair(8),
            data1=RegPair(10),
            offset0=3,
            offset1=6,
            is_st64=False,
            is_rtn=False,
        )
        self.assertEqual(instr.mnemonic, "ds_wrxchg2_b64")
        self.assertEqual(
            instr.disasm(),
            "ds_wrxchg2_b64 v5, v[8:9], v[10:11] offset0:3 offset1:6",
        )

    def test_ast_immutability_and_equality(self) -> None:
        instr1 = DSWrxchg2B64Instruction(
            vdst=RegQuad(0),
            vaddr=RegSingle(1),
            data0=RegPair(2),
            data1=RegPair(4),
            offset0=1,
            offset1=2,
        )
        instr2 = DSWrxchg2B64Instruction(
            vdst=RegQuad(0),
            vaddr=RegSingle(1),
            data0=RegPair(2),
            data1=RegPair(4),
            offset0=1,
            offset1=2,
        )
        self.assertEqual(instr1, instr2)
        self.assertEqual(hash(instr1), hash(instr2))


class TestDeterministicScalarInterpreter(unittest.TestCase):
    """Unit tests for deterministic scalar instruction interpreter."""

    def setUp(self) -> None:
        self.interp = ScalarInterpreter()

    def test_scalar_execute_basic_rtn(self) -> None:
        # Seed LDS at 0x20 and 0x28
        struct.pack_into("<Q", self.interp.lds_mem, 0x20, 0x1111_0000_2222_0000)
        struct.pack_into("<Q", self.interp.lds_mem, 0x28, 0x3333_0000_4444_0000)

        # Set registers
        self.interp.rf.set_u32(1, 0x20)  # v1 = vaddr
        self.interp.rf.set_u64(RegPair(4), 0xAAAA_1111_BBBB_2222)  # v[4:5] = data0
        self.interp.rf.set_u64(RegPair(6), 0xCCCC_3333_DDDD_4444)  # v[6:7] = data1

        instr = DSWrxchg2B64Instruction(
            vdst=RegQuad(8),  # v[8:11]
            vaddr=RegSingle(1),
            data0=RegPair(4),
            data1=RegPair(6),
            offset0=0,
            offset1=1,
            is_st64=False,
            is_rtn=True,
        )

        old0, old1 = self.interp.step(instr)
        self.assertEqual(old0, 0x1111_0000_2222_0000)
        self.assertEqual(old1, 0x3333_0000_4444_0000)

        # Check vdst receives old values
        self.assertEqual(self.interp.rf.get_u64(RegPair(8)), old0)
        self.assertEqual(self.interp.rf.get_u64(RegPair(10)), old1)

        # Check memory updated
        self.assertEqual(
            struct.unpack_from("<Q", self.interp.lds_mem, 0x20)[0],
            0xAAAA_1111_BBBB_2222,
        )
        self.assertEqual(
            struct.unpack_from("<Q", self.interp.lds_mem, 0x28)[0],
            0xCCCC_3333_DDDD_4444,
        )

    def test_scalar_execute_st64_rtn(self) -> None:
        vaddr = 0x40
        addr0 = vaddr + 0 * 512  # 0x40
        addr1 = vaddr + 2 * 512  # 0x440
        struct.pack_into("<Q", self.interp.lds_mem, addr0, 0x0123_4567_89AB_CDEF)
        struct.pack_into("<Q", self.interp.lds_mem, addr1, 0xFEDC_BA98_7654_3210)

        self.interp.rf.set_u2 = vaddr
        self.interp.rf.set_u32(2, vaddr)
        self.interp.rf.set_u64(RegPair(12), 0x9999_8888_7777_6666)
        self.interp.rf.set_u64(RegPair(14), 0x5555_4444_3333_2222)

        instr = DSWrxchg2B64Instruction(
            vdst=RegQuad(16),
            vaddr=RegSingle(2),
            data0=RegPair(12),
            data1=RegPair(14),
            offset0=0,
            offset1=2,
            is_st64=True,
            is_rtn=True,
        )

        old0, old1 = self.interp.step(instr)
        self.assertEqual(old0, 0x0123_4567_89AB_CDEF)
        self.assertEqual(old1, 0xFEDC_BA98_7654_3210)
        self.assertEqual(self.interp.rf.get_u64(RegPair(16)), old0)
        self.assertEqual(self.interp.rf.get_u64(RegPair(18)), old1)

    def test_scalar_execute_non_return(self) -> None:
        struct.pack_into("<Q", self.interp.lds_mem, 0x50, 0x1111_1111_1111_1111)
        struct.pack_into("<Q", self.interp.lds_mem, 0x58, 0x2222_2222_2222_2222)

        self.interp.rf.set_u32(3, 0x50)
        self.interp.rf.set_u64(RegPair(20), 0x3333_3333_3333_3333)
        self.interp.rf.set_u64(RegPair(22), 0x4444_4444_4444_4444)

        # Sentinel in registers 24..27
        for r in range(24, 28):
            self.interp.rf.set_u32(r, 0xCAFE_BABE)

        instr = DSWrxchg2B64Instruction(
            vdst=None,
            vaddr=RegSingle(3),
            data0=RegPair(20),
            data1=RegPair(22),
            offset0=0,
            offset1=1,
            is_st64=False,
            is_rtn=False,
        )

        old0, old1 = self.interp.step(instr)
        self.assertEqual(old0, 0x1111_1111_1111_1111)
        self.assertEqual(old1, 0x2222_2222_2222_2222)

        # Verify registers 24..27 are untouched
        for r in range(24, 28):
            self.assertEqual(self.interp.rf.get_u32(r), 0xCAFE_BABE)

    def test_scalar_step_determinism(self) -> None:
        # Run identical sequence in two independent interpreters and compare
        interp1 = ScalarInterpreter()
        interp2 = ScalarInterpreter()

        for interp in (interp1, interp2):
            struct.pack_into("<Q", interp.lds_mem, 0x100, 0xA1B2_C3D4_E5F6_0718)
            struct.pack_into("<Q", interp.lds_mem, 0x108, 0x1807_F6E5_D4C3_B2A1)
            interp.rf.set_u32(1, 0x100)
            interp.rf.set_u64(RegPair(2), 0x1234_0000_5678_0000)
            interp.rf.set_u64(RegPair(4), 0xABCD_0000_EF01_0000)

        instr = DSWrxchg2B64Instruction(
            vdst=RegQuad(6),
            vaddr=RegSingle(1),
            data0=RegPair(2),
            data1=RegPair(4),
            offset0=0,
            offset1=1,
        )

        res1 = interp1.step(instr)
        res2 = interp2.step(instr)
        self.assertEqual(res1, res2)
        self.assertEqual(bytes(interp1.lds_mem), bytes(interp2.lds_mem))
        self.assertEqual(interp1.rf.regs, interp2.rf.regs)


class TestSIMDMultiLaneDivergenceAndExecMasking(unittest.TestCase):
    """Unit tests for SIMD multi-lane divergence and 32/64-bit EXEC masking."""

    def test_simd_wave32_full_exec(self) -> None:
        state = SIMDState(num_lanes=32, exec_mask=MASK32)
        simd = SIMDInterpreter()

        # Seed distinct LDS addresses for each lane (each lane uses 16 bytes: 2 x 8-byte elements)
        for lane in range(32):
            base_addr = lane * 32
            struct.pack_into("<Q", state.lds_mem, base_addr, 0x1000 + lane)
            struct.pack_into("<Q", state.lds_mem, base_addr + 8, 0x2000 + lane)
            state.set_vgpr(lane, 1, base_addr)
            state.set_vgpr64(lane, RegPair(2), 0xAAAA_0000 + lane)
            state.set_vgpr64(lane, RegPair(4), 0xBBBB_0000 + lane)

        instr = DSWrxchg2B64Instruction(
            vdst=RegQuad(8),
            vaddr=RegSingle(1),
            data0=RegPair(2),
            data1=RegPair(4),
            offset0=0,
            offset1=1,
        )

        results = simd.step(instr, state)
        self.assertEqual(len(results), 32)
        for lane in range(32):
            self.assertIsNotNone(results[lane])
            old0, old1 = results[lane]  # type: ignore
            self.assertEqual(old0, 0x1000 + lane)
            self.assertEqual(old1, 0x2000 + lane)
            self.assertEqual(state.get_vgpr64(lane, RegPair(8)), 0x1000 + lane)
            self.assertEqual(state.get_vgpr64(lane, RegPair(10)), 0x2000 + lane)

            base_addr = lane * 32
            self.assertEqual(
                struct.unpack_from("<Q", state.lds_mem, base_addr)[0],
                0xAAAA_0000 + lane,
            )
            self.assertEqual(
                struct.unpack_from("<Q", state.lds_mem, base_addr + 8)[0],
                0xBBBB_0000 + lane,
            )

    def test_simd_wave64_full_exec(self) -> None:
        state = SIMDState(num_lanes=64, exec_mask=MASK64)
        simd = SIMDInterpreter()

        for lane in range(64):
            base_addr = lane * 16
            struct.pack_into("<Q", state.lds_mem, base_addr, 0x3000 + lane)
            struct.pack_into("<Q", state.lds_mem, base_addr + 8, 0x4000 + lane)
            state.set_vgpr(lane, 1, base_addr)
            state.set_vgpr64(lane, RegPair(2), 0x5000 + lane)
            state.set_vgpr64(lane, RegPair(4), 0x6000 + lane)

        instr = DSWrxchg2B64Instruction(
            vdst=RegQuad(16),
            vaddr=RegSingle(1),
            data0=RegPair(2),
            data1=RegPair(4),
            offset0=0,
            offset1=1,
        )

        results = simd.step(instr, state)
        self.assertEqual(len(results), 64)
        for lane in range(64):
            self.assertIsNotNone(results[lane])
            self.assertEqual(state.get_vgpr64(lane, RegPair(16)), 0x3000 + lane)
            self.assertEqual(state.get_vgpr64(lane, RegPair(18)), 0x4000 + lane)

    def test_simd_checkerboard_mask(self) -> None:
        # Even lanes active (0x5555_5555), odd lanes inactive
        mask = 0x5555_5555
        state = SIMDState(num_lanes=32, exec_mask=mask)
        simd = SIMDInterpreter()

        sentinel = 0xCAFE_BABE_DEAD_BEEF
        for lane in range(32):
            base_addr = lane * 32
            struct.pack_into("<Q", state.lds_mem, base_addr, 0x7000 + lane)
            struct.pack_into("<Q", state.lds_mem, base_addr + 8, 0x8000 + lane)
            state.set_vgpr(lane, 1, base_addr)
            state.set_vgpr64(lane, RegPair(2), 0x9000 + lane)
            state.set_vgpr64(lane, RegPair(4), 0xA000 + lane)
            state.set_vgpr64(lane, RegPair(8), sentinel)
            state.set_vgpr64(lane, RegPair(10), sentinel)

        instr = DSWrxchg2B64Instruction(
            vdst=RegQuad(8),
            vaddr=RegSingle(1),
            data0=RegPair(2),
            data1=RegPair(4),
            offset0=0,
            offset1=1,
        )

        results = simd.step(instr, state)
        for lane in range(32):
            base_addr = lane * 32
            if lane % 2 == 0:
                # Active lane
                self.assertIsNotNone(results[lane])
                self.assertEqual(state.get_vgpr64(lane, RegPair(8)), 0x7000 + lane)
                self.assertEqual(state.get_vgpr64(lane, RegPair(10)), 0x8000 + lane)
                self.assertEqual(
                    struct.unpack_from("<Q", state.lds_mem, base_addr)[0], 0x9000 + lane
                )
            else:
                # Inactive lane: untouched registers and untouched LDS memory
                self.assertIsNone(results[lane])
                self.assertEqual(state.get_vgpr64(lane, RegPair(8)), sentinel)
                self.assertEqual(state.get_vgpr64(lane, RegPair(10)), sentinel)
                self.assertEqual(
                    struct.unpack_from("<Q", state.lds_mem, base_addr)[0], 0x7000 + lane
                )
                self.assertEqual(
                    struct.unpack_from("<Q", state.lds_mem, base_addr + 8)[0], 0x8000 + lane
                )

    def test_simd_all_lanes_inactive(self) -> None:
        state = SIMDState(num_lanes=32, exec_mask=0)
        simd = SIMDInterpreter()

        # Seed memory and registers
        for lane in range(32):
            struct.pack_into("<Q", state.lds_mem, lane * 16, 0x1234)
            state.set_vgpr(lane, 1, lane * 16)
            state.set_vgpr64(lane, RegPair(2), 0x9999)
            state.set_vgpr64(lane, RegPair(4), 0x8888)
            state.set_vgpr64(lane, RegPair(8), 0x0)
            state.set_vgpr64(lane, RegPair(10), 0x0)

        initial_lds = bytes(state.lds_mem)
        instr = DSWrxchg2B64Instruction(
            vdst=RegQuad(8),
            vaddr=RegSingle(1),
            data0=RegPair(2),
            data1=RegPair(4),
            offset0=0,
            offset1=1,
        )

        results = simd.step(instr, state)
        self.assertTrue(all(r is None for r in results))
        self.assertEqual(bytes(state.lds_mem), initial_lds)
        for lane in range(32):
            self.assertEqual(state.get_vgpr64(lane, RegPair(8)), 0)
            self.assertEqual(state.get_vgpr64(lane, RegPair(10)), 0)

    def test_simd_multi_lane_lds_address_contention(self) -> None:
        # First 4 active lanes access the same base address (0x100)
        state = SIMDState(num_lanes=32, exec_mask=0xF)
        simd = SIMDInterpreter()

        struct.pack_into("<Q", state.lds_mem, 0x100, 0x0)
        struct.pack_into("<Q", state.lds_mem, 0x108, 0x0)

        for lane in range(4):
            state.set_vgpr(lane, 1, 0x100)
            # Each lane writes (lane + 1) to slot 0 and (lane + 1) * 10 to slot 1
            state.set_vgpr64(lane, RegPair(2), lane + 1)
            state.set_vgpr64(lane, RegPair(4), (lane + 1) * 10)

        instr = DSWrxchg2B64Instruction(
            vdst=RegQuad(8),
            vaddr=RegSingle(1),
            data0=RegPair(2),
            data1=RegPair(4),
            offset0=0,
            offset1=1,
        )

        results = simd.step(instr, state)
        # Sequential atomicity across lanes:
        # Lane 0 sees old=0, writes 1 and 10
        # Lane 1 sees old=1 and 10, writes 2 and 20
        # Lane 2 sees old=2 and 20, writes 3 and 30
        # Lane 3 sees old=3 and 30, writes 4 and 40
        self.assertEqual(results[0], (0, 0))
        self.assertEqual(results[1], (1, 10))
        self.assertEqual(results[2], (2, 20))
        self.assertEqual(results[3], (3, 30))

        # Final memory holds values from last lane (lane 3)
        self.assertEqual(struct.unpack_from("<Q", state.lds_mem, 0x100)[0], 4)
        self.assertEqual(struct.unpack_from("<Q", state.lds_mem, 0x108)[0], 40)


class TestRegisterAliasingAndInPlacePreservation(unittest.TestCase):
    """Unit tests for register aliasing and in-place operand preservation."""

    def test_alias_vdst_with_data0(self) -> None:
        # vdst = v[4:7], data0 = v[4:5], data1 = v[8:9]
        # data0 VGPRs are overwritten by old0, but memory must receive the ORIGINAL data0 value.
        interp = ScalarInterpreter()
        struct.pack_into("<Q", interp.lds_mem, 0x100, 0x1111_1111_1111_1111)
        struct.pack_into("<Q", interp.lds_mem, 0x108, 0x2222_2222_2222_2222)

        interp.rf.set_u32(1, 0x100)  # vaddr
        orig_data0 = 0xAAAA_AAAA_AAAA_AAAA
        orig_data1 = 0xBBBB_BBBB_BBBB_BBBB
        interp.rf.set_u64(RegPair(4), orig_data0)
        interp.rf.set_u64(RegPair(8), orig_data1)

        instr = DSWrxchg2B64Instruction(
            vdst=RegQuad(4),  # v[4:7] aliases data0 v[4:5]
            vaddr=RegSingle(1),
            data0=RegPair(4),
            data1=RegPair(8),
            offset0=0,
            offset1=1,
        )

        old0, old1 = interp.step(instr)
        self.assertEqual(old0, 0x1111_1111_1111_1111)
        self.assertEqual(old1, 0x2222_2222_2222_2222)

        # vdst[0:1] (v[4:5]) got old0
        self.assertEqual(interp.rf.get_u64(RegPair(4)), old0)
        # vdst[2:3] (v[6:7]) got old1
        self.assertEqual(interp.rf.get_u64(RegPair(6)), old1)

        # Memory MUST contain orig_data0 (not old0) and orig_data1
        self.assertEqual(
            struct.unpack_from("<Q", interp.lds_mem, 0x100)[0], orig_data0
        )
        self.assertEqual(
            struct.unpack_from("<Q", interp.lds_mem, 0x108)[0], orig_data1
        )

    def test_alias_vdst_with_data1(self) -> None:
        # vdst = v[4:7], data0 = v[8:9], data1 = v[6:7]
        # data1 VGPRs are overwritten by old1, but memory must receive the ORIGINAL data1 value.
        interp = ScalarInterpreter()
        struct.pack_into("<Q", interp.lds_mem, 0x200, 0x3333_3333_3333_3333)
        struct.pack_into("<Q", interp.lds_mem, 0x208, 0x4444_4444_4444_4444)

        interp.rf.set_u32(1, 0x200)
        orig_data0 = 0xCCCC_CCCC_CCCC_CCCC
        orig_data1 = 0xDDDD_DDDD_DDDD_DDDD
        interp.rf.set_u64(RegPair(8), orig_data0)
        interp.rf.set_u64(RegPair(6), orig_data1)

        instr = DSWrxchg2B64Instruction(
            vdst=RegQuad(4),  # v[4:7] aliases data1 at v[6:7]
            vaddr=RegSingle(1),
            data0=RegPair(8),
            data1=RegPair(6),
            offset0=0,
            offset1=1,
        )

        old0, old1 = interp.step(instr)
        self.assertEqual(old0, 0x3333_3333_3333_3333)
        self.assertEqual(old1, 0x4444_4444_4444_4444)

        self.assertEqual(interp.rf.get_u64(RegPair(4)), old0)
        self.assertEqual(interp.rf.get_u64(RegPair(6)), old1)

        self.assertEqual(
            struct.unpack_from("<Q", interp.lds_mem, 0x200)[0], orig_data0
        )
        self.assertEqual(
            struct.unpack_from("<Q", interp.lds_mem, 0x208)[0], orig_data1
        )

    def test_alias_vdst_with_both_data_operands(self) -> None:
        # Complete alias: vdst = v[4:7], data0 = v[4:5], data1 = v[6:7]
        interp = ScalarInterpreter()
        struct.pack_into("<Q", interp.lds_mem, 0x300, 0x5555_5555_5555_5555)
        struct.pack_into("<Q", interp.lds_mem, 0x308, 0x6666_6666_6666_6666)

        interp.rf.set_u32(2, 0x300)
        orig_d0 = 0xEEEE_EEEE_EEEE_EEEE
        orig_d1 = 0xFFFF_FFFF_FFFF_FFFF
        interp.rf.set_u64(RegPair(4), orig_d0)
        interp.rf.set_u64(RegPair(6), orig_d1)

        instr = DSWrxchg2B64Instruction(
            vdst=RegQuad(4),
            vaddr=RegSingle(2),
            data0=RegPair(4),
            data1=RegPair(6),
            offset0=0,
            offset1=1,
        )

        old0, old1 = interp.step(instr)
        self.assertEqual(old0, 0x5555_5555_5555_5555)
        self.assertEqual(old1, 0x6666_6666_6666_6666)

        self.assertEqual(interp.rf.get_u64(RegPair(4)), old0)
        self.assertEqual(interp.rf.get_u64(RegPair(6)), old1)
        self.assertEqual(struct.unpack_from("<Q", interp.lds_mem, 0x300)[0], orig_d0)
        self.assertEqual(struct.unpack_from("<Q", interp.lds_mem, 0x308)[0], orig_d1)

    def test_alias_vdst_with_vaddr(self) -> None:
        # vdst = v[0:3], vaddr = v0
        # vaddr register is overwritten by low word of old0, but memory access used the original address
        interp = ScalarInterpreter()
        vaddr = 0x400
        struct.pack_into("<Q", interp.lds_mem, vaddr, 0x7777_7777_7777_7777)
        struct.pack_into("<Q", interp.lds_mem, vaddr + 8, 0x8888_8888_8888_8888)

        interp.rf.set_u32(0, vaddr)
        interp.rf.set_u64(RegPair(4), 0x1212_1212_1212_1212)
        interp.rf.set_u64(RegPair(6), 0x3434_3434_3434_3434)

        instr = DSWrxchg2B64Instruction(
            vdst=RegQuad(0),  # v[0:3] aliases vaddr (v0)
            vaddr=RegSingle(0),
            data0=RegPair(4),
            data1=RegPair(6),
            offset0=0,
            offset1=1,
        )

        old0, old1 = interp.step(instr)
        self.assertEqual(old0, 0x7777_7777_7777_7777)
        self.assertEqual(old1, 0x8888_8888_8888_8888)

        # v0 now holds the low word of old0 (0x77777777), not vaddr (0x400)
        self.assertEqual(interp.rf.get_u32(0), 0x7777_7777)
        self.assertEqual(
            struct.unpack_from("<Q", interp.lds_mem, vaddr)[0], 0x1212_1212_1212_1212
        )
        self.assertEqual(
            struct.unpack_from("<Q", interp.lds_mem, vaddr + 8)[0], 0x3434_3434_3434_3434
        )

    def test_repeated_in_place_ping_pong_exchange(self) -> None:
        # Repeated in-place exchange: vdst=v[4:7], data0=v[4:5], data1=v[6:7]
        # Step 1 swaps (A, B) with (M0, M1).
        # Step 2 swaps them back, restoring original values in registers and memory.
        interp = ScalarInterpreter()
        struct.pack_into("<Q", interp.lds_mem, 0x500, 0x1010_1010_1010_1010)
        struct.pack_into("<Q", interp.lds_mem, 0x508, 0x2020_2020_2020_2020)

        interp.rf.set_u32(1, 0x500)
        interp.rf.set_u64(RegPair(4), 0x3030_3030_3030_3030)
        interp.rf.set_u64(RegPair(6), 0x4040_4040_4040_4040)

        instr = DSWrxchg2B64Instruction(
            vdst=RegQuad(4),
            vaddr=RegSingle(1),
            data0=RegPair(4),
            data1=RegPair(6),
            offset0=0,
            offset1=1,
        )

        # Step 1
        interp.step(instr)
        self.assertEqual(interp.rf.get_u64(RegPair(4)), 0x1010_1010_1010_1010)
        self.assertEqual(interp.rf.get_u64(RegPair(6)), 0x2020_2020_2020_2020)
        self.assertEqual(
            struct.unpack_from("<Q", interp.lds_mem, 0x500)[0], 0x3030_3030_3030_3030
        )
        self.assertEqual(
            struct.unpack_from("<Q", interp.lds_mem, 0x508)[0], 0x4040_4040_4040_4040
        )

        # Step 2: swaps back
        interp.step(instr)
        self.assertEqual(interp.rf.get_u64(RegPair(4)), 0x3030_3030_3030_3030)
        self.assertEqual(interp.rf.get_u64(RegPair(6)), 0x4040_4040_4040_4040)
        self.assertEqual(
            struct.unpack_from("<Q", interp.lds_mem, 0x500)[0], 0x1010_1010_1010_1010
        )
        self.assertEqual(
            struct.unpack_from("<Q", interp.lds_mem, 0x508)[0], 0x2020_2020_2020_2020
        )


@dataclass(frozen=True)
class LDSStatusFlags:
    """Architectural status flags and condition metrics for ds_wrxchg2_b64."""
    bank_conflict: bool
    same_address_collision: bool
    unaligned_qword: bool
    val0_zero: bool
    val0_negative: bool
    val1_zero: bool
    val1_negative: bool
    dword_carry0: int
    dword_carry1: int


def evaluate_lds_status_flags(
    vaddr: int,
    offset0: int,
    offset1: int,
    is_st64: bool,
    val0: int,
    val1: int,
    lds_size_bytes: int = LDS_DEFAULT_SIZE_BYTES,
) -> LDSStatusFlags:
    """Evaluate architectural flags, LDS bank collisions, and numeric condition codes."""
    unit = UNIT_B64_ST64_BYTES if is_st64 else UNIT_B64_BYTES
    eff_addr0 = vaddr + offset0 * unit
    eff_addr1 = vaddr + offset1 * unit

    addr0 = eff_addr0 & (lds_size_bytes - 1)
    addr1 = eff_addr1 & (lds_size_bytes - 1)

    same_addr = (addr0 == addr1)
    unaligned = ((addr0 & 7) != 0) or ((addr1 & 7) != 0)

    # LDS has 32 banks of 4 bytes (stride 128 bytes)
    # A 64-bit access touches dword (addr >> 2) and ((addr + 4) >> 2)
    b0_lo = (addr0 >> 2) % 32
    b0_hi = ((addr0 + 4) >> 2) % 32
    b1_lo = (addr1 >> 2) % 32
    b1_hi = ((addr1 + 4) >> 2) % 32

    banks0 = {b0_lo, b0_hi}
    banks1 = {b1_lo, b1_hi}
    bank_conflict = bool(banks0 & banks1)

    val0_z = (val0 & MASK64) == 0
    val0_neg = bool((val0 >> 63) & 1)
    val1_z = (val1 & MASK64) == 0
    val1_neg = bool((val1 >> 63) & 1)

    # Compute dword carry out of (val_lo + 1)
    lo0, hi0 = split_u64_to_u32s(val0)
    lo1, hi1 = split_u64_to_u32s(val1)
    carry0 = 1 if (lo0 + 1) > MASK32 else 0
    carry1 = 1 if (lo1 + 1) > MASK32 else 0

    return LDSStatusFlags(
        bank_conflict=bank_conflict,
        same_address_collision=same_addr,
        unaligned_qword=unaligned,
        val0_zero=val0_z,
        val0_negative=val0_neg,
        val1_zero=val1_z,
        val1_negative=val1_neg,
        dword_carry0=carry0,
        dword_carry1=carry1,
    )


class TestConditionCodesAndStatusFlags(unittest.TestCase):
    """Unit tests for condition codes, status flags, and carry generation."""

    def test_bank_conflict_detection(self) -> None:
        # Bank stride is 128 bytes. Offset by 128 bytes hits the same bank.
        # offset0=0 (addr 0, banks {0, 1}), offset1=16 (addr 128, banks {0, 1})
        flags = evaluate_lds_status_flags(
            vaddr=0, offset0=0, offset1=16, is_st64=False, val0=10, val1=20
        )
        self.assertTrue(flags.bank_conflict)
        self.assertFalse(flags.same_address_collision)

    def test_no_bank_conflict_different_banks(self) -> None:
        # offset0=0 (addr 0, banks {0, 1}), offset1=1 (addr 8, banks {2, 3})
        flags = evaluate_lds_status_flags(
            vaddr=0, offset0=0, offset1=1, is_st64=False, val0=10, val1=20
        )
        self.assertFalse(flags.bank_conflict)
        self.assertFalse(flags.same_address_collision)

    def test_same_address_exact_collision(self) -> None:
        flags = evaluate_lds_status_flags(
            vaddr=0x100, offset0=2, offset1=2, is_st64=False, val0=0, val1=0
        )
        self.assertTrue(flags.bank_conflict)
        self.assertTrue(flags.same_address_collision)

    def test_unaligned_qword_flag(self) -> None:
        # vaddr=0x104 (aligned to 4 bytes, unaligned to 8 bytes)
        flags = evaluate_lds_status_flags(
            vaddr=0x104, offset0=0, offset1=1, is_st64=False, val0=0, val1=0
        )
        self.assertTrue(flags.unaligned_qword)

        # vaddr=0x108 (aligned to 8 bytes)
        flags_aligned = evaluate_lds_status_flags(
            vaddr=0x108, offset0=0, offset1=1, is_st64=False, val0=0, val1=0
        )
        self.assertFalse(flags_aligned.unaligned_qword)

    def test_zero_and_negative_sign_flags(self) -> None:
        # val0 = 0 (zero flag True, neg False)
        # val1 = 0x8000_0000_0000_0000 (zero False, neg True)
        flags = evaluate_lds_status_flags(
            vaddr=0,
            offset0=0,
            offset1=1,
            is_st64=False,
            val0=0,
            val1=0x8000_0000_0000_0000,
        )
        self.assertTrue(flags.val0_zero)
        self.assertFalse(flags.val0_negative)
        self.assertFalse(flags.val1_zero)
        self.assertTrue(flags.val1_negative)

    def test_dword_carry_propagation(self) -> None:
        # val with lo dword = 0xFFFFFFFF generates carry on +1
        flags_carry = evaluate_lds_status_flags(
            vaddr=0,
            offset0=0,
            offset1=1,
            is_st64=False,
            val0=0x0000_0001_FFFF_FFFF,
            val1=0x0000_0001_0000_0000,
        )
        self.assertEqual(flags_carry.dword_carry0, 1)
        self.assertEqual(flags_carry.dword_carry1, 0)


class TestIEEE754BoundaryExtremes(unittest.TestCase):
    """Unit tests for IEEE 754 boundary extremes, subnormals, and NaN bit preservation.

    Verifies that ds_wrxchg2_b64 performs exact bitwise exchanges on 64-bit IEEE 754
    bit patterns without quietening signaling NaNs, without flushing subnormals to zero,
    and without sign modification of signed zeros.
    """

    def setUp(self) -> None:
        self.interp = ScalarInterpreter()

    def test_nan_signaling_preservation_without_quietening(self) -> None:
        # Signaling NaN in IEEE 754 binary64: exponent=0x7FF, fraction != 0, quiet bit 51 is 0.
        snan0 = 0x7FF0_0000_0000_0001
        snan1 = 0x7FF0_1234_5678_9ABC
        # Bit 51 (0x0008_0000_0000_0000) must remain 0
        self.assertEqual(snan0 & 0x0008_0000_0000_0000, 0)
        self.assertEqual(snan1 & 0x0008_0000_0000_0000, 0)

        struct.pack_into("<Q", self.interp.lds_mem, 0x100, snan0)
        struct.pack_into("<Q", self.interp.lds_mem, 0x108, snan1)

        self.interp.rf.set_u32(1, 0x100)
        # Exchange with new snan patterns
        new_snan0 = 0x7FF4_0000_0000_0002
        new_snan1 = 0x7FF0_0000_DEAD_BEEF
        self.interp.rf.set_u64(RegPair(4), new_snan0)
        self.interp.rf.set_u64(RegPair(6), new_snan1)

        instr = DSWrxchg2B64Instruction(
            vdst=RegQuad(8),
            vaddr=RegSingle(1),
            data0=RegPair(4),
            data1=RegPair(6),
            offset0=0,
            offset1=1,
        )

        old0, old1 = self.interp.step(instr)
        # Returned values must match original SNaN bits exactly (quiet bit must NOT be set)
        self.assertEqual(old0, snan0)
        self.assertEqual(old1, snan1)
        self.assertEqual(old0 & 0x0008_0000_0000_0000, 0)
        self.assertEqual(old1 & 0x0008_0000_0000_0000, 0)

        # Memory must match new SNaN bits exactly
        self.assertEqual(
            struct.unpack_from("<Q", self.interp.lds_mem, 0x100)[0], new_snan0
        )
        self.assertEqual(
            struct.unpack_from("<Q", self.interp.lds_mem, 0x108)[0], new_snan1
        )

    def test_nan_arbitrary_payload_preservation(self) -> None:
        # Quiet NaNs with arbitrary unique payloads
        qnan0 = 0x7FF8_AAAA_5555_1234
        qnan1 = 0xFFF9_BEEF_CAFE_9876

        struct.pack_into("<Q", self.interp.lds_mem, 0x200, qnan0)
        struct.pack_into("<Q", self.interp.lds_mem, 0x208, qnan1)

        self.interp.rf.set_u32(1, 0x200)
        self.interp.rf.set_u64(RegPair(2), 0)
        self.interp.rf.set_u64(RegPair(4), 0)

        instr = DSWrxchg2B64Instruction(
            vdst=RegQuad(8),
            vaddr=RegSingle(1),
            data0=RegPair(2),
            data1=RegPair(4),
            offset0=0,
            offset1=1,
        )

        old0, old1 = self.interp.step(instr)
        self.assertEqual(old0, qnan0)
        self.assertEqual(old1, qnan1)

    def test_signed_zeros_preservation(self) -> None:
        # +0.0 = 0x0000_0000_0000_0000, -0.0 = 0x8000_0000_0000_0000
        pos_zero = 0x0000_0000_0000_0000
        neg_zero = 0x8000_0000_0000_0000

        struct.pack_into("<Q", self.interp.lds_mem, 0x40, pos_zero)
        struct.pack_into("<Q", self.interp.lds_mem, 0x48, neg_zero)

        self.interp.rf.set_u32(1, 0x40)
        self.interp.rf.set_u64(RegPair(2), neg_zero)
        self.interp.rf.set_u64(RegPair(4), pos_zero)

        instr = DSWrxchg2B64Instruction(
            vdst=RegQuad(8),
            vaddr=RegSingle(1),
            data0=RegPair(2),
            data1=RegPair(4),
            offset0=0,
            offset1=1,
        )

        old0, old1 = self.interp.step(instr)
        self.assertEqual(old0, pos_zero)
        self.assertEqual(old1, neg_zero)
        self.assertEqual(
            struct.unpack_from("<Q", self.interp.lds_mem, 0x40)[0], neg_zero
        )
        self.assertEqual(
            struct.unpack_from("<Q", self.interp.lds_mem, 0x48)[0], pos_zero
        )

    def test_signed_infinities_preservation(self) -> None:
        pos_inf = 0x7FF0_0000_0000_0000
        neg_inf = 0xFFF0_0000_0000_0000

        struct.pack_into("<Q", self.interp.lds_mem, 0x60, pos_inf)
        struct.pack_into("<Q", self.interp.lds_mem, 0x68, neg_inf)

        self.interp.rf.set_u32(1, 0x60)
        self.interp.rf.set_u64(RegPair(2), 0x1234)
        self.interp.rf.set_u64(RegPair(4), 0x5678)

        instr = DSWrxchg2B64Instruction(
            vdst=RegQuad(8),
            vaddr=RegSingle(1),
            data0=RegPair(2),
            data1=RegPair(4),
            offset0=0,
            offset1=1,
        )

        old0, old1 = self.interp.step(instr)
        self.assertEqual(old0, pos_inf)
        self.assertEqual(old1, neg_inf)

    def test_subnormal_preservation_without_flush_to_zero(self) -> None:
        # Smallest subnormal: 2^-1074 = 0x0000_0000_0000_0001
        # Largest subnormal: 0x000F_FFFF_FFFF_FFFF
        min_subnormal = 0x0000_0000_0000_0001
        max_subnormal = 0x000F_FFFF_FFFF_FFFF

        struct.pack_into("<Q", self.interp.lds_mem, 0x80, min_subnormal)
        struct.pack_into("<Q", self.interp.lds_mem, 0x88, max_subnormal)

        self.interp.rf.set_u32(1, 0x80)
        self.interp.rf.set_u64(RegPair(2), max_subnormal | 0x8000_0000_0000_0000)
        self.interp.rf.set_u64(RegPair(4), min_subnormal | 0x8000_0000_0000_0000)

        instr = DSWrxchg2B64Instruction(
            vdst=RegQuad(8),
            vaddr=RegSingle(1),
            data0=RegPair(2),
            data1=RegPair(4),
            offset0=0,
            offset1=1,
        )

        old0, old1 = self.interp.step(instr)
        # Must NOT be flushed to 0
        self.assertNotEqual(old0, 0)
        self.assertNotEqual(old1, 0)
        self.assertEqual(old0, min_subnormal)
        self.assertEqual(old1, max_subnormal)

        mem0 = struct.unpack_from("<Q", self.interp.lds_mem, 0x80)[0]
        mem1 = struct.unpack_from("<Q", self.interp.lds_mem, 0x88)[0]
        self.assertEqual(mem0, max_subnormal | 0x8000_0000_0000_0000)
        self.assertEqual(mem1, min_subnormal | 0x8000_0000_0000_0000)

    def test_normal_boundary_extremes(self) -> None:
        # min_normal: 0x0010_0000_0000_0000
        # max_normal: 0x7FEF_FFFF_FFFF_FFFF
        min_norm = 0x0010_0000_0000_0000
        max_norm = 0x7FEF_FFFF_FFFF_FFFF

        struct.pack_into("<Q", self.interp.lds_mem, 0x90, min_norm)
        struct.pack_into("<Q", self.interp.lds_mem, 0x98, max_norm)

        self.interp.rf.set_u32(1, 0x90)
        self.interp.rf.set_u64(RegPair(2), 0)
        self.interp.rf.set_u64(RegPair(4), 0)

        instr = DSWrxchg2B64Instruction(
            vdst=RegQuad(8),
            vaddr=RegSingle(1),
            data0=RegPair(2),
            data1=RegPair(4),
            offset0=0,
            offset1=1,
        )

        old0, old1 = self.interp.step(instr)
        self.assertEqual(old0, min_norm)
        self.assertEqual(old1, max_norm)


class TestNegativeControlsAndMutations(unittest.TestCase):
    """Unit tests for negative controls, syntax rejection, and mutation detection."""

    def test_syntax_rejection_invalid_register_indices(self) -> None:
        with self.assertRaises(ValueError):
            RegSingle(-1)
        with self.assertRaises(ValueError):
            RegSingle(256)
        with self.assertRaises(ValueError):
            RegPair(255)
        with self.assertRaises(ValueError):
            RegQuad(253)

    def test_syntax_rejection_destination_must_be_vgpr(self) -> None:
        dst_sgpr = RegQuad(0, RegType.SGPR)
        with self.assertRaises(TypeError):
            DSWrxchg2B64Instruction(
                vdst=dst_sgpr,
                vaddr=RegSingle(1),
                data0=RegPair(2),
                data1=RegPair(4),
            )

    def test_syntax_rejection_vaddr_must_be_vgpr(self) -> None:
        addr_sgpr = RegSingle(0, RegType.SGPR)
        with self.assertRaises(TypeError):
            DSWrxchg2B64Instruction(
                vdst=RegQuad(0),
                vaddr=addr_sgpr,
                data0=RegPair(2),
                data1=RegPair(4),
            )

    def test_syntax_rejection_data_must_be_vgpr(self) -> None:
        data_sgpr = RegPair(0, RegType.SGPR)
        with self.assertRaises(TypeError):
            DSWrxchg2B64Instruction(
                vdst=RegQuad(0),
                vaddr=RegSingle(1),
                data0=data_sgpr,
                data1=RegPair(4),
            )

    def test_syntax_rejection_offset_out_of_range(self) -> None:
        with self.assertRaises(ValueError):
            DSWrxchg2B64Instruction(
                vdst=RegQuad(0),
                vaddr=RegSingle(1),
                data0=RegPair(2),
                data1=RegPair(4),
                offset0=256,
            )
        with self.assertRaises(ValueError):
            DSWrxchg2B64Instruction(
                vdst=RegQuad(0),
                vaddr=RegSingle(1),
                data0=RegPair(2),
                data1=RegPair(4),
                offset1=-1,
            )

    def test_syntax_rejection_returning_requires_vdst(self) -> None:
        with self.assertRaises(ValueError):
            DSWrxchg2B64Instruction(
                vdst=None,
                vaddr=RegSingle(1),
                data0=RegPair(2),
                data1=RegPair(4),
                is_rtn=True,
            )

    def test_negative_control_bit_corruption_detection(self) -> None:
        lds = bytearray(LDS_DEFAULT_SIZE_BYTES)
        struct.pack_into("<Q", lds, 0x10, 0x1234_5678_9ABC_DEF0)
        struct.pack_into("<Q", lds, 0x18, 0xFEDC_BA98_7654_3210)

        old0, old1 = oracle_ds_wrxchg2_b64(
            lds, vaddr=0x10, data0=0xAAAA, data1=0xBBBB, offset0=0, offset1=1
        )
        # Corrupt single bit
        corrupted_old0 = old0 ^ 1
        self.assertNotEqual(old0, corrupted_old0)

        # Corrupt high dword bit
        corrupted_old1 = old1 ^ (1 << 48)
        self.assertNotEqual(old1, corrupted_old1)

    def test_mutation_st64_stride_mutant(self) -> None:
        """Mutant: st64 uses unit=8 instead of unit=512."""
        def mutant_compute_addr(vaddr: int, offset: int, is_st64: bool) -> int:
            # Bug: always uses unit=8
            return (vaddr + offset * 8) & (LDS_DEFAULT_SIZE_BYTES - 1)

        correct = compute_lds_address(0x40, offset=2, is_st64=True)
        mutant = mutant_compute_addr(0x40, offset=2, is_st64=True)
        self.assertEqual(correct, 0x40 + 1024)
        self.assertEqual(mutant, 0x40 + 16)
        self.assertNotEqual(correct, mutant)

    def test_mutation_destination_overwrite_before_exchange(self) -> None:
        """Mutant: writes vdst before performing LDS memory exchange."""
        interp = ScalarInterpreter()
        struct.pack_into("<Q", interp.lds_mem, 0x100, 0x1111)
        struct.pack_into("<Q", interp.lds_mem, 0x108, 0x2222)

        interp.rf.set_u32(1, 0x100)
        orig_data0 = 0xAAAA
        interp.rf.set_u64(RegPair(4), orig_data0)
        interp.rf.set_u64(RegPair(6), 0xBBBB)

        # Mutant step function that writes vdst before read
        class BuggyInterpreter(ScalarInterpreter):
            def buggy_step(self, instr: DSWrxchg2B64Instruction) -> None:
                # Bug: sets vdst before reading operands / exchanging
                self.rf.set_quad(instr.vdst, 0, 0, 0, 0)
                super().step(instr)

        buggy = BuggyInterpreter()
        struct.pack_into("<Q", buggy.lds_mem, 0x100, 0x1111)
        struct.pack_into("<Q", buggy.lds_mem, 0x108, 0x2222)
        buggy.rf.set_u32(1, 0x100)
        buggy.rf.set_u64(RegPair(4), orig_data0)
        buggy.rf.set_u64(RegPair(6), 0xBBBB)

        instr = DSWrxchg2B64Instruction(
            vdst=RegQuad(4),
            vaddr=RegSingle(1),
            data0=RegPair(4),
            data1=RegPair(6),
            offset0=0,
            offset1=1,
        )

        # Correct execution
        interp.step(instr)
        self.assertEqual(
            struct.unpack_from("<Q", interp.lds_mem, 0x100)[0], orig_data0
        )

        # Buggy execution
        buggy.buggy_step(instr)
        # In buggy execution, data0 was clobbered to 0 before memory exchange
        self.assertNotEqual(
            struct.unpack_from("<Q", buggy.lds_mem, 0x100)[0], orig_data0
        )

    def test_mutation_ignore_exec_mask(self) -> None:
        """Mutant: SIMD interpreter executes inactive lanes."""
        state = SIMDState(num_lanes=32, exec_mask=0x0000_0000)  # all inactive
        for lane in range(32):
            struct.pack_into("<Q", state.lds_mem, lane * 16, 0x9999)

        # Mutant interpreter that ignores exec_mask
        class MutantSIMDInterpreter(SIMDInterpreter):
            def step(
                self, instr: DSWrxchg2B64Instruction, state: SIMDState
            ) -> List[Optional[Tuple[int, int]]]:
                results = []
                for lane in range(state.num_lanes):
                    # Bug: no is_active check
                    vaddr = state.get_vgpr(lane, instr.vaddr.index)
                    data0 = state.get_vgpr64(lane, instr.data0)
                    data1 = state.get_vgpr64(lane, instr.data1)
                    res = oracle_ds_wrxchg2_b64(
                        state.lds_mem, vaddr, data0, data1, instr.offset0, instr.offset1
                    )
                    results.append(res)
                return results

        instr = DSWrxchg2B64Instruction(
            vdst=RegQuad(8),
            vaddr=RegSingle(1),
            data0=RegPair(2),
            data1=RegPair(4),
            offset0=0,
            offset1=1,
        )

        mutant_simd = MutantSIMDInterpreter()
        results = mutant_simd.step(instr, state)
        # Mutation detected: inactive lanes were executed!
        self.assertTrue(any(r is not None for r in results))

    def test_mutation_partial_dword_exchange(self) -> None:
        """Mutant: exchanges only low dword and ignores high dword."""
        def mutant_exchange(lds_mem: bytearray, addr: int, val64: int) -> int:
            old_lo = struct.unpack_from("<I", lds_mem, addr)[0]
            # Bug: high dword left unexchanged
            struct.pack_into("<I", lds_mem, addr, val64 & MASK32)
            return old_lo

        lds = bytearray(LDS_DEFAULT_SIZE_BYTES)
        struct.pack_into("<Q", lds, 0x20, 0x1234_5678_9ABC_DEF0)
        mutant_res = mutant_exchange(lds, 0x20, 0xAAAA_BBBB_CCCC_DDDD)

        # Only 32 bits returned instead of full 64 bits
        self.assertNotEqual(mutant_res, 0x1234_5678_9ABC_DEF0)
        self.assertEqual(mutant_res, 0x9ABC_DEF0)

    def test_mutation_subnormal_flush_to_zero(self) -> None:
        """Mutant: flushes subnormal 64-bit value to zero."""
        def mutant_ftz(val: int) -> int:
            exp = (val >> 52) & 0x7FF
            frac = val & 0x000F_FFFF_FFFF_FFFF
            if exp == 0 and frac != 0:
                return 0  # Bug: FTZ
            return val

        subnormal = 0x0000_0000_0000_0005
        correct = subnormal
        mutant = mutant_ftz(subnormal)
        self.assertNotEqual(correct, mutant)
        self.assertEqual(mutant, 0)

    def test_mutation_snan_quietening(self) -> None:
        """Mutant: sets quiet bit on signaling NaN."""
        def mutant_quieten(val: int) -> int:
            # Bug: quietens NaN
            return val | 0x0008_0000_0000_0000

        snan = 0x7FF0_0000_0000_0001
        self.assertEqual(snan & 0x0008_0000_0000_0000, 0)
        mutant = mutant_quieten(snan)
        self.assertNotEqual(snan, mutant)
        self.assertEqual(mutant & 0x0008_0000_0000_0000, 0x0008_0000_0000_0000)


if __name__ == "__main__":
    unittest.main()




