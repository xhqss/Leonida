"""Texel offsets of image_sample*_o / image_gather4*_o in tools/rdna2spirv.py: the shape of the SPIR-V.

These tests feed the translator hand-written ``.s`` listings, so they need neither llvm-mc nor spirv-tools.

docs/design.md, "Texel offsets": the first address dword packs signed 6-bit offsets, x = [5:0], y = [13:8],
z = [21:16], which become the Offset image operand. Sampled values are checked on a GPU by
runtime/test/shaders/ps_texoff.asm. That test cannot see the sign extension on a driver that itself keeps only the
low six bits of an Offset: on an AMD Radeon RX 6800 (driver 26.11.2) a translator that read the fields unsigned
(60 for -4) sampled the same texels. So the extraction is pinned down here: OpBitFieldSExtract, bit 8k, 6 bits wide.
"""
import re

import pytest

from listing import translate_listing

# v10 = the offset dword, v11.. = the coordinates (and LOD); v3 is what listing.EPILOGUE exports.
PROLOGUE = "\n".join(f"v_mov_b32 v{r}, 0" for r in (3, 10, 11, 12, 13, 14))
DESC = "s[8:15], s[16:19]"
FORMS = [   # (instruction, SPIR-V op that takes the Offset, offset components)
    (f"image_sample_lz_o v[4:5], v[10:12], {DESC} dmask:0x3 dim:SQ_RSRC_IMG_2D", "OpImageSampleExplicitLod", 2),
    (f"image_sample_l_o v[4:5], v[10:13], {DESC} dmask:0x3 dim:SQ_RSRC_IMG_2D", "OpImageSampleExplicitLod", 2),
    (f"image_sample_o v[4:5], v[10:12], {DESC} dmask:0x3 dim:SQ_RSRC_IMG_2D", "OpImageSampleImplicitLod", 2),
    (f"image_sample_lz_o v[4:5], v[10:13], {DESC} dmask:0x3 dim:SQ_RSRC_IMG_3D", "OpImageSampleExplicitLod", 3),
    (f"image_sample_o v[4:5], v[10:11], {DESC} dmask:0x3 dim:SQ_RSRC_IMG_2D a16", "OpImageSampleImplicitLod", 2),
    (f"image_sample_lz_o v[4:5], v[10:11], {DESC} dmask:0x3 dim:SQ_RSRC_IMG_2D a16", "OpImageSampleExplicitLod", 2),
    (f"image_sample_l_o v[4:5], v[10:12], {DESC} dmask:0x3 dim:SQ_RSRC_IMG_2D a16", "OpImageSampleExplicitLod", 2),
    (f"image_sample_b_o v[4:5], v[10:12], {DESC} dmask:0x3 dim:SQ_RSRC_IMG_2D a16", "OpImageSampleImplicitLod", 2),
    (f"image_sample_c_lz_o v4, v[10:12], {DESC} dmask:0x1 dim:SQ_RSRC_IMG_2D a16", "OpImageSampleDrefExplicitLod", 2),
    (f"image_sample_c_o v4, v[10:13], {DESC} dmask:0x1 dim:SQ_RSRC_IMG_2D", "OpImageSampleDrefImplicitLod", 2),
    (f"image_sample_c_b_o v4, v[10:14], {DESC} dmask:0x1 dim:SQ_RSRC_IMG_2D", "OpImageSampleDrefImplicitLod", 2),
    (f"image_sample_c_o v4, v[10:12], {DESC} dmask:0x1 dim:SQ_RSRC_IMG_2D a16", "OpImageSampleDrefImplicitLod", 2),
    (f"image_sample_cl_o v[4:5], v[10:13], {DESC} dmask:0x3 dim:SQ_RSRC_IMG_2D", "OpImageSampleImplicitLod", 2),
    (f"image_sample_c_b_cl_o v4, v[10:15], {DESC} dmask:0x1 dim:SQ_RSRC_IMG_2D", "OpImageSampleDrefImplicitLod", 2),
    (f"image_gather4_lz_o v[4:7], v[10:12], {DESC} dmask:0x1 dim:SQ_RSRC_IMG_2D", "OpImageGather", 2),
]


def offset_fields(spv, op):
    """[(first bit, width, signed)] of the Offset operand shared by every numeric path for ``op``."""
    consts = {m[1]: int(m[2]) for m in re.finditer(r"(%\w+) = OpConstant %uint (\d+)", spv)}
    lines = [ln for ln in spv.splitlines() if f"= {op} " in ln]
    assert lines and all("Offset" in ln and "ConstOffset" not in ln for ln in lines), lines
    # Typed gathers have one path per numeric class. Each path must apply the same offset.
    def offset_operand(line):
        toks = line.split()
        mask = next(i for i, t in enumerate(toks) if "Offset" in t.split("|"))
        return toks[mask + 1 + toks[mask].split("|").index("Offset")]    # operands follow the mask in mask order
    offsets = {offset_operand(line) for line in lines}
    assert len(offsets) == 1, lines
    vec = offsets.pop()
    parts = re.search(rf"{re.escape(vec)} = OpCompositeConstruct %v\dint (.+)", spv)[1].split()
    fields = []
    for part in parts:
        bits = re.search(rf"{re.escape(part)} = OpBitcast %int (%\w+)", spv)[1]
        ext = re.search(rf"{re.escape(bits)} = OpBitField([SU])Extract %uint %\w+ (%\w+) (%\w+)", spv)
        fields.append((consts[ext[2]], consts[ext[3]], ext[1] == "S"))
    return fields


@pytest.mark.parametrize("asm,op,n", FORMS, ids=[f[0].split()[0] + "_" + f[0].rsplit("_", 1)[1] for f in FORMS])
def test_offset_is_signed_six_bit_fields_at_byte_steps(asm, op, n, tmp_path):
    spv = translate_listing(tmp_path, PROLOGUE + "\n" + asm)
    assert offset_fields(spv, op) == [(8 * k, 6, True) for k in range(n)]


def test_sample_without_o_has_no_offset(tmp_path):
    spv = translate_listing(tmp_path, PROLOGUE + f"\nimage_sample_lz v[4:5], v[11:12], {DESC} dmask:0x3 dim:SQ_RSRC_IMG_2D")
    assert "Offset" not in "".join(ln for ln in spv.splitlines(True) if "OpImageSample" in ln)


# ---- --texel-offset-fallback: the offset folded into the coordinate, no VK_KHR_maintenance8 ----

import shutil  # noqa: E402
import subprocess  # noqa: E402

import rdna2spirv  # noqa: E402  (put on sys.path by listing)

SAMPLES = [f for f in FORMS if f[1] != "OpImageGather"]


@pytest.fixture
def fallback(monkeypatch):
    monkeypatch.setattr(rdna2spirv, "TEXEL_OFFSET_FALLBACK", True)


def _shift_fields(spv):
    """Fields of the offset vector the fallback converts to float (OpConvertSToF of the OpCompositeConstruct)."""
    consts = {m[1]: int(m[2]) for m in re.finditer(r"(%\w+) = OpConstant %uint (\d+)", spv)}
    for m in re.finditer(r"%\w+ = OpConvertSToF %(?:v\d)?float (%\w+)", spv):
        src = m[1]
        c = re.search(rf"{re.escape(src)} = OpCompositeConstruct %v\dint (.+)", spv)
        parts = c[1].split() if c else [src]
        fields = []
        for part in parts:
            b = re.search(rf"{re.escape(part)} = OpBitcast %int (%\w+)", spv)
            ext = b and re.search(rf"{re.escape(b[1])} = OpBitField([SU])Extract %uint %\w+ (%\w+) (%\w+)", spv)
            if not ext:
                break
            fields.append((consts[ext[2]], consts[ext[3]], ext[1] == "S"))
        else:
            return fields
    raise AssertionError("no offset vector converted to float")


@pytest.mark.parametrize("asm,op,n", SAMPLES, ids=[f[0].split()[0] + "_" + f[0].rsplit("_", 1)[1] for f in SAMPLES])
def test_fallback_folds_the_offset(asm, op, n, tmp_path, fallback):
    spv = translate_listing(tmp_path, PROLOGUE + "\n" + asm)
    samples = [ln for ln in spv.splitlines() if "OpImageSample" in ln]
    assert samples and not any(" Offset " in ln for ln in samples), samples
    assert _shift_fields(spv) == [(8 * k, 6, True) for k in range(n)]
    assert "OpImageQuerySizeLod" in spv and "OpImageQueryLevels" in spv
    assert re.search(r"= OpFDiv %(?:v\d)?float %\w+ %\w+", spv) and "OpCapability ImageQuery" in spv
    implicit = asm.startswith(("image_sample_o ", "image_sample_b_o ", "image_sample_c_o ", "image_sample_c_b_o ",
                               "image_sample_cl_o ", "image_sample_c_b_cl_o "))
    assert ("OpImageQueryLod" in spv) == implicit
    if implicit:
        # Grad with the derivatives of the unshifted coordinate: same LOD as the sample without the offset.
        assert "OpImageSampleImplicitLod" not in spv and "OpImageSampleDrefImplicitLod" not in spv
        if asm.startswith("image_sample_c_"):
            assert re.search(r"OpImageSampleDrefExplicitLod %float %\w+ %\w+ %\w+ Grad(\|MinLod)? %\w+ %\w+( %\w+)?$", samples[0])
        else:
            assert re.search(r"OpImageSampleExplicitLod %v4float %\w+ %\w+ Grad(\|MinLod)? %\w+ %\w+( %\w+)?$", samples[0])
        assert ("MinLod" in samples[0]) == ("_cl_o " in asm)
        assert "OpDPdx" in spv and "OpDPdy" in spv
        assert ("Exp2" in spv) == asm.startswith(("image_sample_b_o ", "image_sample_c_b_o ", "image_sample_c_b_cl_o "))
    assert "OpCapability ImageGatherExtended" not in spv


def test_fallback_keeps_gather_offsets(tmp_path, fallback):
    gather, op, n = FORMS[-1]
    spv = translate_listing(tmp_path, PROLOGUE + "\n" + gather)
    assert offset_fields(spv, op) == [(8 * k, 6, True) for k in range(n)]
    assert "OpCapability ImageGatherExtended" in spv


@pytest.mark.skipif(not (shutil.which("spirv-as") and shutil.which("spirv-val")), reason="needs spirv-as and spirv-val")
@pytest.mark.parametrize("asm,op,n", SAMPLES, ids=[f[0].split()[0] + "_" + f[0].rsplit("_", 1)[1] for f in SAMPLES])
def test_fallback_needs_no_maintenance8(asm, op, n, tmp_path, fallback):
    """spirv-val without --allow-offset-texture-operand: the module needs no VK_KHR_maintenance8."""
    spv = translate_listing(tmp_path, PROLOGUE + "\n" + asm)
    src, out = tmp_path / "m.spvasm", tmp_path / "m.spv"
    src.write_text(spv, encoding="utf-8")
    for cmd in (["spirv-as", "--target-env", "vulkan1.2", str(src), "-o", str(out)],
                ["spirv-val", "--target-env", "vulkan1.2", str(out)]):
        r = subprocess.run(cmd, capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr
