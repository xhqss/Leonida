"""Pure-Python SPIR-V binary assembler (standard library only, no external spirv-as).

Assembles SPIR-V assembly text (.spvasm) emitted by tools/rdna2spirv.py (or any
conforming SPIR-V assembly targeting Vulkan 1.0–1.4) directly into binary SPIR-V
words in memory.

Grammar tables in tools/spirv_asm_tables.py are derived from Khronos Group
SPIRV-Headers (MIT License): spirv.core.grammar.json and
extinst.glsl.std.450.grammar.json.

As a module:
    from spirv_asm import assemble
    spv_bytes = assemble(asm_text, target_env="vulkan1.2")

As a CLI:
    python3 -I tools/spirv_asm.py input.spvasm -o output.spv [--target-env vulkan1.2]
"""
import argparse
import array
import os
import re
import struct
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from spirv_asm_tables import BIT_ENUMS, GLSL_STD_450, INSTRUCTIONS, VALUE_ENUMS  # noqa: E402

SPIRV_MAGIC = 0x07230203

TARGET_ENV_VERSION = {
    "vulkan1.0": 0x00010000,
    "vulkan1.1": 0x00010300,
    "vulkan1.2": 0x00010500,
    "vulkan1.3": 0x00010600,
    "vulkan1.4": 0x00010600,
    "spv1.0": 0x00010000,
    "spv1.1": 0x00010100,
    "spv1.2": 0x00010200,
    "spv1.3": 0x00010300,
    "spv1.4": 0x00010400,
    "spv1.5": 0x00010500,
    "spv1.6": 0x00010600,
}

_ID_KINDS = frozenset(("IdRef", "IdResult", "IdResultType", "IdScope", "IdMemorySemantics"))
_EXT_SETS = {
    "GLSL.std.450": GLSL_STD_450,
}

# Precompute fixed-arity pure-Id instructions (e.g. OpIAdd, OpFMul, OpSelect, OpStore, OpBranch)
# so the hot loop encodes ~80% of shader lines without per-operand dispatch overhead.
_PURE_ID_INSNS: dict[str, tuple[int, bool, bool, int, bool]] = {}
for _opname, (_opc, _hrt, _hres, _ospecs) in INSTRUCTIONS.items():
    if _opname in ("OpSwitch", "OpExtInst"):
        continue
    if all(_k in _ID_KINDS and _q in ("", "*") for _k, _q in _ospecs):
        _has_var = bool(_ospecs and _ospecs[-1][1] == "*")
        if not _has_var or all(_q == "" for _, _q in _ospecs[:-1]):
            _min_args = len(_ospecs) - (1 if _has_var else 0)
            _PURE_ID_INSNS[_opname] = (_opc, _hrt, _hres, _min_args, _has_var)


class SpirvAsmError(ValueError):
    """Raised when SPIR-V assembly text cannot be encoded."""


def _tokenize_line(line: str, lineno: int) -> list[str]:
    """Split one assembly line into tokens, stripping ';' comments outside quoted strings."""
    if '"' not in line:
        semi = line.find(";")
        return (line[:semi] if semi >= 0 else line).split()
    tokens = []
    n = len(line)
    i = 0
    while i < n:
        ch = line[i]
        if ch <= " ":
            i += 1
            continue
        if ch == ";":
            break
        if ch == '"':
            j = i + 1
            out = ['"']
            while j < n:
                c = line[j]
                if c == "\\" and j + 1 < n:
                    out.append(c)
                    out.append(line[j + 1])
                    j += 2
                elif c == '"':
                    out.append('"')
                    j += 1
                    break
                else:
                    out.append(c)
                    j += 1
            else:
                raise SpirvAsmError(f"line {lineno}: unterminated string literal")
            tokens.append("".join(out))
            i = j
            continue
        j = i + 1
        while j < n and line[j] > " " and line[j] != ";":
            j += 1
        tokens.append(line[i:j])
        i = j
    return tokens


def _unquote_string(tok: str, lineno: int) -> str:
    if len(tok) < 2 or tok[0] != '"' or tok[-1] != '"':
        raise SpirvAsmError(f"line {lineno}: expected string literal, got {tok!r}")
    raw = tok[1:-1]
    if "\\" not in raw:
        return raw
    # SPIRV-Tools decodes \<ch> as the literal character <ch> (e.g. \n -> 'n', \" -> '"', \\ -> '\').
    out = []
    i = 0
    n = len(raw)
    while i < n:
        if raw[i] == "\\" and i + 1 < n:
            out.append(raw[i + 1])
            i += 2
        else:
            out.append(raw[i])
            i += 1
    return "".join(out)


def _encode_string_words(text: str) -> list[int]:
    raw = text.encode("utf-8") + b"\x00"
    rem = len(raw) & 3
    if rem:
        raw += b"\x00" * (4 - rem)
    return list(struct.unpack(f"<{len(raw) // 4}I", raw))


def _parse_int(tok: str, lineno: int) -> int:
    try:
        return int(tok, 0)
    except ValueError as exc:
        raise SpirvAsmError(f"line {lineno}: invalid integer literal {tok!r}") from exc


_HEX_FLOAT_RE = re.compile(r"^([+-]?)0[xX]([0-9a-fA-F]*)(?:\.([0-9a-fA-F]*))?[pP]([+-]?\d+)$")
_FLOAT_SPEC = {
    16: (5, 10, 15, "<e", "<H"),
    32: (8, 23, 127, "<f", "<I"),
    64: (11, 52, 1023, "<d", "<Q"),
}


def _parse_float_bits(tok: str, width: int, lineno: int) -> int:
    """Encode a SPIR-V float literal (decimal or width-aware hex-float incl. Inf/NaN) as IEEE-754 bits."""
    spec = _FLOAT_SPEC.get(width)
    if spec is None:
        raise SpirvAsmError(f"line {lineno}: unsupported float constant width {width}")
    exp_bits, mant_bits, bias, pack_fmt, unpack_fmt = spec
    exp_max = (1 << exp_bits) - 1
    m = _HEX_FLOAT_RE.match(tok)
    if m:
        sign_s, ipart, fpart, exp_s = m.groups()
        fpart = fpart or ""
        if not ipart and not fpart:
            raise SpirvAsmError(f"line {lineno}: invalid float literal {tok!r}")
        sign = 1 if sign_s == "-" else 0
        sig = int(ipart + fpart, 16)
        if sig == 0:
            return sign << (exp_bits + mant_bits)
        exp = int(exp_s) - 4 * len(fpart)
        top = sig.bit_length() - 1
        e_biased = exp + top + bias
        if e_biased == exp_max:
            # SPIRV-Tools hex-float spelling for Inf (0x1p+128) and NaN payloads (0x1.8p+128).
            frac = sig & ((1 << top) - 1)
            if top <= mant_bits:
                mant = frac << (mant_bits - top)
            else:
                mant = frac >> (top - mant_bits)
            return (sign << (exp_bits + mant_bits)) | (exp_max << mant_bits) | mant
        if e_biased > exp_max:
            return (sign << (exp_bits + mant_bits)) | (exp_max << mant_bits)
        if 0 < e_biased < exp_max and top <= mant_bits:
            frac = sig & ((1 << top) - 1)
            mant = frac << (mant_bits - top)
            return (sign << (exp_bits + mant_bits)) | (e_biased << mant_bits) | mant
        try:
            fv = float.fromhex(tok)
            return struct.unpack(unpack_fmt, struct.pack(pack_fmt, fv))[0]
        except OverflowError:
            return (sign << (exp_bits + mant_bits)) | (exp_max << mant_bits)
        except ValueError as exc:
            raise SpirvAsmError(f"line {lineno}: invalid float literal {tok!r}") from exc
    try:
        fv = float(tok)
    except ValueError as exc:
        raise SpirvAsmError(f"line {lineno}: invalid float literal {tok!r}") from exc
    try:
        return struct.unpack(unpack_fmt, struct.pack(pack_fmt, fv))[0]
    except OverflowError:
        sign = 1 if tok.startswith("-") else 0
        return (sign << (exp_bits + mant_bits)) | (exp_max << mant_bits)


def _encode_integer(val: int, width: int, signedness: int) -> list[int]:
    # A hexadecimal bit pattern for a signed narrow value still needs a sign-extended word.
    if signedness and 0 < width < 32 and (1 << (width - 1)) <= val < (1 << width):
        val -= 1 << width
    if width == 64:
        return [val & 0xFFFFFFFF, (val >> 32) & 0xFFFFFFFF]
    return [val & 0xFFFFFFFF]


def _encode_context_number(tok: str, type_info, lineno: int) -> list[int]:
    if type_info is None:
        raise SpirvAsmError(f"line {lineno}: OpConstant references unknown scalar type")
    kind, width = type_info[0], type_info[1]
    if kind == "int":
        if width <= 32 or width == 64:
            return _encode_integer(_parse_int(tok, lineno), width, type_info[2])
        raise SpirvAsmError(f"line {lineno}: unsupported integer constant width {width}")
    if kind == "float":
        bits = _parse_float_bits(tok, width, lineno)
        if width <= 32:
            return [bits & 0xFFFFFFFF]
        if width == 64:
            return [bits & 0xFFFFFFFF, (bits >> 32) & 0xFFFFFFFF]
        raise SpirvAsmError(f"line {lineno}: unsupported float constant width {width}")
    raise SpirvAsmError(f"line {lineno}: unsupported constant type {type_info!r}")


def assemble(text: str, target_env: str = "vulkan1.2") -> bytes:
    """Assemble SPIR-V text into binary SPIR-V bytes for `target_env`."""
    version = TARGET_ENV_VERSION.get(target_env)
    if version is None:
        raise SpirvAsmError(f"unsupported target environment: {target_env!r}")

    sym_to_id: dict[str, int] = {}
    next_id = 1
    type_info: dict[int, tuple] = {}       # type_id -> ('int', width, signedness) | ('float', width)
    id_to_type: dict[int, int] = {}        # result_id -> result_type_id
    ext_imports: dict[int, dict[str, int]] = {}  # ext_set_id -> {name: opcode}

    def get_id(tok: str, lineno: int) -> int:
        nonlocal next_id
        if not tok or tok[0] != "%":
            raise SpirvAsmError(f"line {lineno}: expected %id, got {tok!r}")
        vid = sym_to_id.get(tok)
        if vid is None:
            vid = next_id
            next_id += 1
            sym_to_id[tok] = vid
        return vid

    words = array.array("I", [SPIRV_MAGIC, version, 0, 0, 0])

    def encode_operand(kind: str, toks: list[str], idx: int, lineno: int,
                       res_type_id: int | None, cur_ext_table: dict[str, int] | None,
                       switch_type: tuple[int, int], out: list[int]) -> int:
        if kind in _ID_KINDS:
            out.append(get_id(toks[idx], lineno))
            return idx + 1
        if kind == "LiteralInteger":
            out.append(_parse_int(toks[idx], lineno) & 0xFFFFFFFF)
            return idx + 1
        if kind == "LiteralFloat":
            out.append(_parse_float_bits(toks[idx], 32, lineno))
            return idx + 1
        if kind == "LiteralString":
            out.extend(_encode_string_words(_unquote_string(toks[idx], lineno)))
            return idx + 1
        if kind == "LiteralContextDependentNumber":
            out.extend(_encode_context_number(toks[idx], type_info.get(res_type_id), lineno))
            return idx + 1
        if kind == "LiteralExtInstInteger":
            tok = toks[idx]
            if cur_ext_table is not None and tok in cur_ext_table:
                out.append(cur_ext_table[tok])
            else:
                out.append(_parse_int(tok, lineno) & 0xFFFFFFFF)
            return idx + 1
        if kind == "PairLiteralIntegerIdRef":
            out.extend(_encode_integer(_parse_int(toks[idx], lineno), *switch_type))
            if idx + 1 >= len(toks):
                raise SpirvAsmError(f"line {lineno}: missing label in {kind}")
            out.append(get_id(toks[idx + 1], lineno))
            return idx + 2
        if kind == "PairIdRefIdRef":
            if idx + 1 >= len(toks):
                raise SpirvAsmError(f"line {lineno}: missing second id in {kind}")
            out.append(get_id(toks[idx], lineno))
            out.append(get_id(toks[idx + 1], lineno))
            return idx + 2
        if kind == "PairIdRefLiteralInteger":
            if idx + 1 >= len(toks):
                raise SpirvAsmError(f"line {lineno}: missing literal in {kind}")
            out.append(get_id(toks[idx], lineno))
            out.append(_parse_int(toks[idx + 1], lineno) & 0xFFFFFFFF)
            return idx + 2
        if kind == "LiteralSpecConstantOpInteger":
            opname = toks[idx]
            sub = INSTRUCTIONS.get(opname) or INSTRUCTIONS.get("Op" + opname)
            if sub is None:
                raise SpirvAsmError(f"line {lineno}: unknown OpSpecConstantOp opcode {opname!r}")
            out.append(sub[0])
            idx += 1
            # Encode remaining operands of the nested opcode
            for sk, sq in sub[3]:
                if sq == "*":
                    while idx < len(toks):
                        idx = encode_operand(sk, toks, idx, lineno, res_type_id, cur_ext_table, switch_type, out)
                elif sq == "?":
                    if idx < len(toks):
                        idx = encode_operand(sk, toks, idx, lineno, res_type_id, cur_ext_table, switch_type, out)
                else:
                    if idx >= len(toks):
                        raise SpirvAsmError(f"line {lineno}: missing operand {sk} for {opname}")
                    idx = encode_operand(sk, toks, idx, lineno, res_type_id, cur_ext_table, switch_type, out)
            return idx

        venum = VALUE_ENUMS.get(kind)
        if venum is not None:
            tok = toks[idx]
            entry = venum.get(tok)
            if entry is None:
                # Allow raw numeric value fallback (e.g. hex enum value)
                if tok[:1].isdigit() or tok.startswith(("0x", "0X")):
                    out.append(_parse_int(tok, lineno) & 0xFFFFFFFF)
                    return idx + 1
                raise SpirvAsmError(f"line {lineno}: unknown {kind} value {tok!r}")
            val, params = entry
            out.append(val)
            idx += 1
            for pk in params:
                if idx >= len(toks):
                    raise SpirvAsmError(f"line {lineno}: missing parameter {pk} for {kind}.{tok}")
                idx = encode_operand(pk, toks, idx, lineno, res_type_id, cur_ext_table, switch_type, out)
            return idx

        benum = BIT_ENUMS.get(kind)
        if benum is not None:
            tok = toks[idx]
            mask = 0
            flag_params: list[tuple[int, tuple[str, ...]]] = []
            for part in tok.split("|"):
                entry = benum.get(part)
                if entry is None:
                    if part[:1].isdigit() or part.startswith(("0x", "0X")):
                        mask |= _parse_int(part, lineno) & 0xFFFFFFFF
                        continue
                    raise SpirvAsmError(f"line {lineno}: unknown {kind} flag {part!r} in {tok!r}")
                bval, params = entry
                mask |= bval
                if params:
                    flag_params.append((bval, params))
            out.append(mask)
            idx += 1
            # SPIR-V spec: bit-mask extra parameters appear in ascending bit-value order.
            flag_params.sort(key=lambda pair: pair[0])
            for _, params in flag_params:
                for pk in params:
                    if idx >= len(toks):
                        raise SpirvAsmError(f"line {lineno}: missing parameter {pk} for {kind} {tok!r}")
                    idx = encode_operand(pk, toks, idx, lineno, res_type_id, cur_ext_table, switch_type, out)
            return idx

        raise SpirvAsmError(f"line {lineno}: unsupported operand kind {kind!r}")

    for lineno, raw_line in enumerate(text.splitlines(), 1):
        toks = _tokenize_line(raw_line, lineno)
        if not toks:
            continue
        res_tok = None
        if len(toks) >= 3 and toks[1] == "=":
            res_tok = toks[0]
            opname = toks[2]
            arg_idx = 3
        else:
            opname = toks[0]
            arg_idx = 1

        fast = _PURE_ID_INSNS.get(opname)
        if fast is not None:
            opcode, has_rt, has_res, min_args, has_var = fast
            if has_res != (res_tok is not None):
                raise SpirvAsmError(
                    f"line {lineno}: {opname} "
                    + ("requires '%id =' result" if has_res else "does not produce a result id"))
            n_rem = len(toks) - arg_idx - (1 if has_rt else 0)
            if n_rem < min_args or (not has_var and n_rem != min_args):
                raise SpirvAsmError(f"line {lineno}: {opname} operand count mismatch (got {n_rem})")
            out = []
            if has_rt:
                rt_id = get_id(toks[arg_idx], lineno)
                out.append(rt_id)
                arg_idx += 1
            else:
                rt_id = None
            if has_res:
                rid = get_id(res_tok, lineno)
                out.append(rid)
                if rt_id is not None:
                    id_to_type[rid] = rt_id
            for k in range(arg_idx, len(toks)):
                out.append(get_id(toks[k], lineno))
            word_count = 1 + len(out)
            if word_count > 0xFFFF:
                raise SpirvAsmError(f"line {lineno}: {opname} word count {word_count} exceeds 65535")
            words.append((word_count << 16) | opcode)
            words.extend(out)
            continue

        spec = INSTRUCTIONS.get(opname)
        if spec is None:
            raise SpirvAsmError(f"line {lineno}: unknown opcode {opname!r}")
        opcode, has_rt, has_res, op_specs = spec

        out: list[int] = []
        res_type_id = None
        res_id = None

        if has_rt:
            if arg_idx >= len(toks):
                raise SpirvAsmError(f"line {lineno}: {opname} missing result type")
            res_type_id = get_id(toks[arg_idx], lineno)
            out.append(res_type_id)
            arg_idx += 1

        if has_res:
            if res_tok is None:
                raise SpirvAsmError(f"line {lineno}: {opname} requires '%id =' result")
            res_id = get_id(res_tok, lineno)
            out.append(res_id)
            if res_type_id is not None:
                id_to_type[res_id] = res_type_id
        elif res_tok is not None:
            raise SpirvAsmError(f"line {lineno}: {opname} does not produce a result id")

        # Track scalar type widths for OpConstant / OpSwitch, and ExtInstImport sets.
        if opname == "OpTypeInt" and res_id is not None and arg_idx + 1 < len(toks):
            type_info[res_id] = ("int", _parse_int(toks[arg_idx], lineno), _parse_int(toks[arg_idx + 1], lineno))
        elif opname == "OpTypeFloat" and res_id is not None and arg_idx < len(toks):
            type_info[res_id] = ("float", _parse_int(toks[arg_idx], lineno))
        elif opname == "OpExtInstImport" and res_id is not None and arg_idx < len(toks):
            ext_name = _unquote_string(toks[arg_idx], lineno)
            if ext_name in _EXT_SETS:
                ext_imports[res_id] = _EXT_SETS[ext_name]

        cur_ext_table = None
        switch_type = (32, 0)
        if opname == "OpExtInst" and arg_idx < len(toks):
            cur_ext_table = ext_imports.get(get_id(toks[arg_idx], lineno))
        elif opname == "OpSwitch" and arg_idx < len(toks):
            sel_id = get_id(toks[arg_idx], lineno)
            sel_ti = type_info.get(id_to_type.get(sel_id))
            if sel_ti and sel_ti[0] == "int":
                switch_type = (sel_ti[1], sel_ti[2])

        for kind, quant in op_specs:
            if quant == "*":
                while arg_idx < len(toks):
                    arg_idx = encode_operand(kind, toks, arg_idx, lineno, res_type_id, cur_ext_table, switch_type, out)
            elif quant == "?":
                if arg_idx < len(toks):
                    arg_idx = encode_operand(kind, toks, arg_idx, lineno, res_type_id, cur_ext_table, switch_type, out)
            else:
                if arg_idx >= len(toks):
                    raise SpirvAsmError(f"line {lineno}: {opname} missing operand ({kind})")
                arg_idx = encode_operand(kind, toks, arg_idx, lineno, res_type_id, cur_ext_table, switch_type, out)

        if arg_idx != len(toks):
            raise SpirvAsmError(f"line {lineno}: {opname} unexpected trailing tokens {toks[arg_idx:]!r}")

        word_count = 1 + len(out)
        if word_count > 0xFFFF:
            raise SpirvAsmError(f"line {lineno}: {opname} word count {word_count} exceeds 65535")
        words.append((word_count << 16) | opcode)
        words.extend(out)

    words[3] = next_id
    if sys.byteorder != "little":
        words.byteswap()
    return words.tobytes()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Pure-Python SPIR-V binary assembler (no spirv-as required).")
    ap.add_argument("src", help="input .spvasm file")
    ap.add_argument("-o", "--out", required=True, help="output .spv binary file")
    ap.add_argument("--target-env", default="vulkan1.2", choices=sorted(TARGET_ENV_VERSION),
                    help="target SPIR-V / Vulkan environment (default: vulkan1.2)")
    args = ap.parse_args(argv)
    try:
        with open(args.src, encoding="utf-8") as f:
            text = f.read()
        data = assemble(text, target_env=args.target_env)
        with open(args.out, "wb") as f:
            f.write(data)
    except (OSError, ValueError, SpirvAsmError) as exc:    # ValueError: a source file that is not UTF-8
        print(f"ERROR: {args.src}: {exc}" if isinstance(exc, UnicodeDecodeError) else f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    try:  # a redirected stdout is strict ANSI code page under -I; write '?' for what it cannot encode
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError, OSError):
        pass
    sys.exit(main())
