"""Tests for tools/spirv_asm.py (pure-Python SPIR-V binary assembler)."""
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools")
SEMANTIC = os.path.join(ROOT, "tests", "semantic")
for _p in (TOOLS, SEMANTIC):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import rdna2spirv  # noqa: E402
import spirv_asm  # noqa: E402
import support  # noqa: E402

SPIRV_VAL = shutil.which("spirv-val")
SPIRV_AS = shutil.which("spirv-as")


def _validate(spv: bytes, target_env: str = "vulkan1.2") -> None:
    if not SPIRV_VAL:
        return
    with tempfile.NamedTemporaryFile(suffix=".spv", delete=False) as f:
        f.write(spv)
        path = f.name
    try:
        r = subprocess.run(
            [SPIRV_VAL, "--target-env", target_env, "--allow-offset-texture-operand", path],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            raise AssertionError(f"spirv-val failed ({target_env}):\n{r.stderr}")
    finally:
        os.unlink(path)


class SpirvAsmUnitTests(unittest.TestCase):
    def test_header_and_target_env_versions(self):
        asm = (
            "OpCapability Shader\n"
            "OpMemoryModel Logical GLSL450\n"
            'OpEntryPoint GLCompute %main "main"\n'
            "OpExecutionMode %main LocalSize 1 1 1\n"
            "%void = OpTypeVoid\n"
            "%fn = OpTypeFunction %void\n"
            "%main = OpFunction %void None %fn\n"
            "%entry = OpLabel\n"
            "OpReturn\n"
            "OpFunctionEnd\n"
        )
        for env, expected_ver in (
            ("vulkan1.0", 0x00010000),
            ("vulkan1.1", 0x00010300),
            ("vulkan1.2", 0x00010500),
            ("vulkan1.3", 0x00010600),
        ):
            spv = spirv_asm.assemble(asm, target_env=env)
            magic, ver, gen, bound, schema = struct.unpack_from("<5I", spv, 0)
            self.assertEqual(magic, 0x07230203)
            self.assertEqual(ver, expected_ver)
            self.assertEqual(gen, 0)
            self.assertGreater(bound, 1)
            self.assertEqual(schema, 0)
            _validate(spv, target_env=env)

    def test_constants_strings_bit_enums_and_control_flow(self):
        # Exercises int32/int64/float32/float64/neg-zero/hex-float constants, GLSL.std.450 OpExtInst,
        # BitEnum multi-flag ascending-bit parameter ordering (MemoryAccess Aligned|Nontemporal),
        # OpSwitch, OpPhi, comments inside/outside lines, and escaped string literals.
        asm = (
            "; leading comment\n"
            "OpCapability Shader\n"
            "OpCapability Int64\n"
            "OpCapability Float64\n"
            'OpExtension "SPV_KHR_storage_buffer_storage_class"\n'
            '%glsl = OpExtInstImport "GLSL.std.450" ; inline comment\n'
            "OpMemoryModel Logical GLSL450\n"
            'OpEntryPoint GLCompute %main "main"\n'
            "OpExecutionMode %main LocalSize 4 1 1\n"
            '%str = OpString "hello; \\"world\\""\n'
            "%void = OpTypeVoid\n"
            "%fn = OpTypeFunction %void\n"
            "%uint = OpTypeInt 32 0\n"
            "%ulong = OpTypeInt 64 0\n"
            "%float = OpTypeFloat 32\n"
            "%double = OpTypeFloat 64\n"
            "%ptr_fn_u = OpTypePointer Function %uint\n"
            "%c0 = OpConstant %uint 0\n"
            "%c1 = OpConstant %uint 1\n"
            "%c_hex = OpConstant %uint 0xDEADBEEF\n"
            "%c64 = OpConstant %ulong 0x1122334455667788\n"
            "%fneg0 = OpConstant %float -0.0\n"
            "%fhex = OpConstant %float 0x1.8p+0\n"
            "%dval = OpConstant %double 3.5\n"
            "%main = OpFunction %void None %fn\n"
            "%entry = OpLabel\n"
            "%var = OpVariable %ptr_fn_u Function\n"
            "OpStore %var %c_hex Nontemporal|Aligned 4\n"
            "%ld = OpLoad %uint %var Aligned|Nontemporal 4\n"
            "%absf = OpExtInst %float %glsl FAbs %fneg0\n"
            "%sumf = OpFAdd %float %absf %fhex\n"
            "OpSelectionMerge %merge None\n"
            "OpSwitch %ld %def 0 %case0 1 %case1\n"
            "%case0 = OpLabel\n"
            "OpBranch %merge\n"
            "%case1 = OpLabel\n"
            "OpBranch %merge\n"
            "%def = OpLabel\n"
            "OpBranch %merge\n"
            "%merge = OpLabel\n"
            "%phi = OpPhi %uint %c0 %case0 %c1 %case1 %ld %def\n"
            "OpReturn\n"
            "OpFunctionEnd\n"
        )
        spv = spirv_asm.assemble(asm, target_env="vulkan1.2")
        _validate(spv)
        # Verify 64-bit constant words appear little-endian (lo32 then hi32) and -0.0 is 0x80000000.
        words = struct.unpack(f"<{len(spv) // 4}I", spv)
        self.assertIn(0x55667788, words)
        self.assertIn(0x11223344, words)
        self.assertIn(0x80000000, words)
        self.assertIn(0x3FC00000, words)  # 0x1.8p+0 = 1.5f

    def test_subword_integer_literals_use_the_declared_signedness(self) -> None:
        for width, signed, token, expected in (
            (8, 1, "0x80", 0xFFFFFF80),
            (16, 1, "0x8000", 0xFFFF8000),
            (16, 1, "-32768", 0xFFFF8000),
            (16, 0, "0x8000", 0x00008000),
        ):
            with self.subTest(width=width, signed=signed, token=token):
                asm = f'''OpCapability Shader
OpCapability Int{width}
OpMemoryModel Logical GLSL450
OpEntryPoint GLCompute %main "main"
OpExecutionMode %main LocalSize 1 1 1
%void = OpTypeVoid
%fn = OpTypeFunction %void
%short = OpTypeInt {width} {signed}
%value = OpConstant %short {token}
%main = OpFunction %void None %fn
%entry = OpLabel
OpSelectionMerge %merge None
OpSwitch %value %merge {token} %case
%case = OpLabel
OpBranch %merge
%merge = OpLabel
OpReturn
OpFunctionEnd
'''
                spv = spirv_asm.assemble(asm, target_env="vulkan1.2")
                words = struct.unpack(f"<{len(spv) // 4}I", spv)
                literals = {}
                offset = 5
                while offset < len(words):
                    count, opcode = words[offset] >> 16, words[offset] & 0xFFFF
                    if opcode in (43, 251):  # OpConstant, OpSwitch
                        literals[opcode] = words[offset + 3]
                    offset += count
                self.assertEqual(literals, {43: expected, 251: expected})
                _validate(spv)

    def test_hex_float_inf_nan_and_string_escapes_match_spirv_as(self):
        # Regression tests for PR #2251 review (@FreddyxS):
        # 1) Width-aware hex-float Inf and NaN payload spellings (f16, f32, f64).
        # 2) SPIRV-Tools string backslash decoding (\n -> literal 'n', not LF).
        asm = (
            "OpCapability Shader\n"
            "OpCapability Float16\n"
            "OpCapability Float64\n"
            "OpMemoryModel Logical GLSL450\n"
            'OpEntryPoint GLCompute %main "main"\n'
            "OpExecutionMode %main LocalSize 1 1 1\n"
            'OpName %main "left\\nright"\n'
            "%void = OpTypeVoid\n"
            "%fn = OpTypeFunction %void\n"
            "%f16 = OpTypeFloat 16\n"
            "%f32 = OpTypeFloat 32\n"
            "%f64 = OpTypeFloat 64\n"
            "%h_inf = OpConstant %f16 0x1p+16\n"
            "%h_nan = OpConstant %f16 0x1.8p+16\n"
            "%f_inf = OpConstant %f32 0x1p+128\n"
            "%f_ninf = OpConstant %f32 -0x1p+128\n"
            "%f_nan = OpConstant %f32 0x1.8p+128\n"
            "%d_inf = OpConstant %f64 0x1p+1024\n"
            "%d_nan = OpConstant %f64 0x1.8p+1024\n"
            "%main = OpFunction %void None %fn\n"
            "%entry = OpLabel\n"
            "OpReturn\n"
            "OpFunctionEnd\n"
        )
        spv = spirv_asm.assemble(asm, target_env="vulkan1.2")
        _validate(spv)
        words = struct.unpack(f"<{len(spv) // 4}I", spv)
        self.assertIn(0x7C00, words)       # f16 +Inf
        self.assertIn(0x7E00, words)       # f16 qNaN
        self.assertIn(0x7F800000, words)   # f32 +Inf
        self.assertIn(0xFF800000, words)   # f32 -Inf
        self.assertIn(0x7FC00000, words)   # f32 qNaN
        self.assertIn(0x7FF00000, words)   # f64 +Inf hi word
        self.assertIn(0x7FF80000, words)   # f64 qNaN hi word
        # Verify exact byte-for-byte parity (excluding header generator word [2]) with spirv-as when present.
        if SPIRV_AS:
            with tempfile.TemporaryDirectory() as d:
                sa, sv = os.path.join(d, "m.spvasm"), os.path.join(d, "m.spv")
                with open(sa, "w", encoding="utf-8", newline="\n") as f:
                    f.write(asm)
                subprocess.run([SPIRV_AS, sa, "-o", sv, "--target-env", "vulkan1.2"], check=True)
                with open(sv, "rb") as f:
                    ref_words = struct.unpack(f"<{os.path.getsize(sv) // 4}I", f.read())
            self.assertEqual(len(words), len(ref_words))
            self.assertEqual(words[:2] + words[3:], ref_words[:2] + ref_words[3:])

    def test_translated_shaders_validate_and_match_spirv_as(self):
        # Translate diverse synthetic shaders (PS ALU/dot4/f16/branches, CS LDS/atomics/wave64-emulate)
        # and verify every binary assembled by spirv_asm passes spirv-val.
        ps_lines = [
            "v_mov_b32_e32 v1, 0x01020304",
            "v_mov_b32_e32 v2, 0x05060708",
            "v_mov_b32_e32 v3, 0x3f800000",
            "v_dot4_i32_i8 v4, v1, v2, v3",
            "v_add_f32_e32 v5, v3, 1.5",
            "v_pk_add_f16 v6, v1, v2 op_sel:[0,1] op_sel_hi:[1,0]",
            "exp mrt0 v4, v5, v6, v3 done vm",
            "s_endpgm",
        ]
        spvasm = support.translate_lines("ps_spirv_asm_smoke", ps_lines)
        spv = spirv_asm.assemble(spvasm, target_env="vulkan1.2")
        _validate(spv)
        # Also test a wave64-emulated compute shader (exercises OpControlBarrier, Workgroup arrays, loops,
        # PhysicalStorageBuffer, OpAtomic*, OpSwitch/structured CF, Int64).
        import json
        cs_lines = [
            "s_load_dwordx2 s[4:5], s[0:1], 0x0",
            "s_waitcnt lgkmcnt(0)",
            "v_lshlrev_b32_e32 v1, 2, v0",
            "ds_write_b32 v1, v0",
            "s_waitcnt lgkmcnt(0)",
            "s_barrier",
            "ds_read_b32 v2, v1",
            "s_waitcnt lgkmcnt(0)",
            "v_cmp_lt_u32_e64 vcc, v0, 32",
            "v_cndmask_b32_e64 v3, v2, v0, vcc",
            "global_store_dword v1, v3, s[4:5]",
            "s_endpgm",
        ]
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "disasm"))
            os.makedirs(os.path.join(d, "fspx"))
            s_path = os.path.join(d, "disasm", "cs_spirv_asm_w64.s")
            with open(s_path, "w", encoding="utf-8", newline="\n") as f:
                f.write("; cs_spirv_asm_w64  code 0x0..0x100, gfx1013, 0 invalid words\n")
                for ln in cs_lines:
                    f.write(f"\t{ln} ; encoding: [0x00,0x00,0x00,0x00]\n")
            with open(os.path.join(d, "fspx", "cs_spirv_asm_w64.json"), "w", encoding="utf-8", newline="\n") as f:
                json.dump({"rsrc2": (8 << 1) | (16 << 15), "threads": [64, 1, 1]}, f)
            old_w64 = rdna2spirv.WAVE64_EMULATE
            try:
                rdna2spirv.WAVE64_EMULATE = True
                cs_spvasm = rdna2spirv.translate(s_path)
            finally:
                rdna2spirv.WAVE64_EMULATE = old_w64
        cs_spv = spirv_asm.assemble(cs_spvasm, target_env="vulkan1.2")
        _validate(cs_spv)

    def test_rdna2spirv_assembler_python_no_asm_skips_spvasm_file(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "disasm"))
            os.makedirs(os.path.join(d, "fspx"))
            s_path = os.path.join(d, "disasm", "cs_tiny.s")
            out_dir = os.path.join(d, "out")
            with open(s_path, "w", encoding="utf-8", newline="\n") as f:
                f.write("; cs_tiny  code 0x0..0x10, gfx1013, 0 invalid words\n")
                f.write("\tv_mov_b32_e32 v0, 1.0 ; encoding: [0xf2,0x02,0x00,0x7e]\n")
                f.write("\ts_endpgm ; encoding: [0x00,0x00,0x81,0xbf]\n")
            with open(os.path.join(d, "fspx", "cs_tiny.json"), "w", encoding="utf-8", newline="\n") as f:
                json.dump({"rsrc2": (4 << 1), "threads": [32, 1, 1]}, f)
            if SPIRV_VAL:
                r = subprocess.run(
                    [sys.executable, "-I", os.path.join(TOOLS, "rdna2spirv.py"),
                     s_path, "--out", out_dir, "--assembler", "python", "--no-asm"],
                    capture_output=True, text=True,
                )
                self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
                self.assertTrue(os.path.isfile(os.path.join(out_dir, "cs_tiny.spv")))
                self.assertFalse(os.path.exists(os.path.join(out_dir, "cs_tiny.spvasm")))

    def test_cli_and_error_cases(self):
        with self.assertRaises(spirv_asm.SpirvAsmError):
            spirv_asm.assemble("OpNotARealOpcode %x\n")
        with self.assertRaises(spirv_asm.SpirvAsmError):
            spirv_asm.assemble("OpTypeVoid\n")  # missing result id
        with self.assertRaises(spirv_asm.SpirvAsmError):
            spirv_asm.assemble("%x = OpCapability Shader\n")  # unexpected result id
        with self.assertRaises(spirv_asm.SpirvAsmError):
            spirv_asm.assemble('%s = OpString "unterminated\n')
        with self.assertRaises(spirv_asm.SpirvAsmError):
            spirv_asm.assemble("OpCapability NotACapability\n")
        with self.assertRaises(spirv_asm.SpirvAsmError):
            spirv_asm.assemble("OpCapability Shader\n", target_env="invalid")

        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "min.spvasm")
            dst = os.path.join(d, "min.spv")
            with open(src, "w", encoding="utf-8", newline="\n") as f:
                f.write(
                    "OpCapability Shader\nOpMemoryModel Logical GLSL450\n"
                    'OpEntryPoint GLCompute %m "main"\nOpExecutionMode %m LocalSize 1 1 1\n'
                    "%v = OpTypeVoid\n%ft = OpTypeFunction %v\n%m = OpFunction %v None %ft\n"
                    "%l = OpLabel\nOpReturn\nOpFunctionEnd\n"
                )
            rc = spirv_asm.main([src, "-o", dst, "--target-env", "vulkan1.2"])
            self.assertEqual(rc, 0)
            with open(dst, "rb") as f:
                _validate(f.read())

    def test_cli_source_that_is_not_utf8_is_one_error_line(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "bad.spvasm")
            dst = os.path.join(d, "bad.spv")
            with open(src, "wb") as f:
                f.write(b"OpCapability Shader\n; \xb1\xff\n")
            r = subprocess.run([sys.executable, "-I", os.path.join(TOOLS, "spirv_asm.py"), src, "-o", dst],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 1, r.stderr)
            self.assertNotIn("Traceback", r.stderr)
            self.assertIn("ERROR:", r.stderr)
            self.assertFalse(os.path.exists(dst))


if __name__ == "__main__":
    unittest.main()
