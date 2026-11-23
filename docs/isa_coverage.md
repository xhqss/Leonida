# RDNA2 ISA coverage of the translator

`tools/isa_coverage.py` compares the instructions of AMD's machine-readable RDNA2 ISA specification with what
`tools/rdna2spirv.py` has a rule for. It reads no game data and needs no GPU; it is a count over the public ISA and
the translator's source.

## Getting the specification

AMD publishes the XML files on GPUOpen, [AMD machine-readable GPU ISA documentation](https://gpuopen.com/machine-readable-isa).
The zip is not in this repository (the RDNA2 file alone is 11 MB):

```sh
curl -L -o isa.zip https://gpuopen.com/download/machine-readable-isa/latest/
unzip isa.zip amdgpu_isa_rdna2.xml
```

The numbers below come from the package `AMD_GPU_MR_ISA_XML_2026_08_06.zip` (SHA-256
`82404f1126761b7877595b622afa7e1f311f2f41e89a3abe9aaf8ad045c082e2`), whose `amdgpu_isa_rdna2.xml` is "AMD RDNA 2",
schema 1.1.1, release date 2026-02-20, and carries `<License>MIT</License>`. AMD's reader for the same files is the
C++ [isa_spec_manager](https://github.com/GPUOpen-Tools/isa_spec_manager) (MIT); the schema is documented there in
`documentation/spec_documentation.md`. This tool parses the XML itself with `xml.etree`.

## Running it

```sh
python -I tools/isa_coverage.py --isa amdgpu_isa_rdna2.xml              # markdown report
python -I tools/isa_coverage.py --isa amdgpu_isa_rdna2.xml --list       # plus the unknown instructions per encoding
python -I tools/isa_coverage.py --isa amdgpu_isa_rdna2.xml --json
python -I tools/isa_coverage.py --isa amdgpu_isa_rdna2.xml --llvm-mc llvm-mc   # classify non-RDNA2 names with llvm-mc
```

`--isa` defaults to `$LEONIDA_RDNA2_ISA_XML`. With that variable set,
`python -I tests/test_isa_coverage.py` also checks the real translator against the real XML; without it those tests
skip and the rest runs on a small hand-written fixture in the same schema.

## What the classes mean

The translator dispatches on the opcode string with one long if/elif chain (`Gen._insn`), so the tool reads the
source with `ast` instead of running it:

| class | meaning |
|---|---|
| handled-exact | the name is compared with `op` (`==`, `in`) or is a key of a mnemonic table (`NOOPS`, `F2`, ...) |
| handled-family | no exact name, but it starts with a prefix `_insn` or a method it hands `op` to tests with `op.startswith`, or ends with a suffix one of their if/elif chains tests with `op.endswith` (alone, or beside a `<stem> in self.TABLE` guard, which limits the family to that table's stems: `s_*_saveexec_b32/b64` and `EXEC_LOGIC`) |
| refused | the name sits in a test whose whole body is `raise Unsupported(...)` |
| unknown | none of these: the final `raise Unsupported(op)` is what it reaches |

Limits. "Handled" means a rule exists, not that the rule is right; the GPU tests and `tests/semantic` do that.
A broad prefix such as `ds_` marks every `ds_*` instruction handled-family even where a sub-dispatch refuses one
form, so read handled-family as an upper bound; the instructions whose branch contains a `raise Unsupported` are
listed separately ("handled but with operand forms that raise"). Names built by string formatting are invisible.
Only the AMD XML defines the denominator: it omits a few gfx1013 instructions that LLVM accepts (below), and lists
some that a desktop part does not execute.

## Result (translator at `41f44826`, AMD XML of 2026-08-06)

Every new translator rule moves these numbers; rerun the tool for the current ones.

1,153 RDNA2 instructions: 436 handled-exact, 408 handled-family, 2 refused (`s_denorm_mode`, `s_round_mode`), 307
unknown. The unknown ones cluster in groups the corpus does not need or that are not written yet: the f64
transcendentals and `v_div_*`, FLAT and SCRATCH memory, the global atomics, the `image_gather4*` variants outside the
four `_l`/`_lz` forms, packed image formats (`*_pck`, `*_by2`), the 16-bit `v_pk_*` integer forms, `v_dot*`, and the
`*_saveexec` / `s_nand`/`s_nor`/`s_xnor` scalar forms. `python -I tools/isa_coverage.py --list` has the full list per
encoding.

Names the translator matches that are not in the RDNA2 XML (16), each checked with
`llvm-mc -arch=amdgcn -mcpu=gfx1013`:

| name | llvm-mc for gfx1013 | verdict |
|---|---|---|
| `s_and_b32_mask`, `s_or_b32_mask` | invalid instruction | dead: nothing in the repository produces them |
| `v_add_u16`, `v_sub_u16` | other GPU only | internal: `v_add_nc_u16` / `v_sub_nc_u16` are re-dispatched under these names |
| `v_subrev_u16`, `v_and_b16`, `v_or_b16`, `v_xor_b16` | other GPU only | dead GFX9 spellings, harmless |
| `s_delay_alu` | other GPU only (RDNA3) | deliberate, in `NOOPS` |
| `v_mac_f32`, `v_mac_legacy_f32`, `v_mad_f32`, `v_mad_legacy_f32`, `v_madak_f32`, `v_madmk_f32`, `s_get_waveid_in_workgroup` | valid | real gfx1013 instructions that AMD's XML omits |

None is a typo that breaks translation, so there is nothing to fix in the translator; the two `_mask` names and the
four dead 16-bit spellings are candidates for removal. No name ends in a suffix `base_op()` strips (`_e32`, `_e64`,
`_sdwa`, `_dpp`), and every `startswith` prefix matches at least one RDNA2 instruction.
