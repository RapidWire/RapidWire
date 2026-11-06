from __future__ import annotations

import dataclasses

import pytest

from rapidwire.contract.assembler import Assembler, ConstRef, assemble
from rapidwire.contract.errors import FormatError, VerificationError
from rapidwire.contract.format import FunctionEntry, Program, encode
from rapidwire.contract.limits import Limits
from rapidwire.contract.opcodes import Op
from rapidwire.contract.types import INT, NONE, STR, Kind, Type, dict_of, list_of
from rapidwire.contract.verifier import verify

MAX = 8000


def ok(body: str = "PUSH_INT 0\nRET", header: str = ".function main public returns=int", extra: str = "") -> str:
    return f"{header}\n{body}\n.end\n{extra}"


def check(data: bytes, message: str, *, limits: Limits | None = None) -> None:
    with pytest.raises(VerificationError, match=message):
        if limits is None:
            verify(data, max_size=MAX)
        else:
            verify(data, max_size=MAX, limits=limits)


def base_program() -> Program:
    return Assembler().function("main", public=True, returns=INT).emit("PUSH_INT", 0).emit("RET").assembler.program()


def test_valid_program_passes() -> None:
    verified = verify(assemble(ok()), max_size=MAX)
    assert verified.public_function("main") is not None
    assert verified.function("main") is verified.functions[0]
    assert verified.functions[0].max_stack == 1
    assert len(verified.sha256) == 64


def test_size_limit_is_inclusive() -> None:
    def sized(total: int) -> bytes:
        program = base_program()
        first = "x" * 4000
        filler = "y" * (total - len(encode(program)) - 3 - (3 + len(first)))
        padded = dataclasses.replace(program, constants=(*program.constants, first, filler))
        data = encode(padded)
        assert len(data) == total
        return data

    assert verify(sized(8000), max_size=8000)
    with pytest.raises(VerificationError, match="8001 bytes, larger than the 8000-byte limit"):
        verify(sized(8001), max_size=8000)


def test_header_errors_are_format_errors() -> None:
    data = assemble(ok())
    with pytest.raises(FormatError, match="bad magic"):
        verify(b"XXXX" + data[4:], max_size=MAX)
    with pytest.raises(FormatError, match="unsupported format version"):
        verify(data[:4] + b"\x02\x00" + data[6:], max_size=MAX)
    with pytest.raises(FormatError, match="add up to"):
        verify(data + b"\x00", max_size=MAX)
    with pytest.raises(FormatError, match="binary must be bytes"):
        verify("x", max_size=MAX)  # type: ignore[arg-type]


def replace_program(**changes: object) -> bytes:
    return encode(dataclasses.replace(base_program(), **changes))  # type: ignore[arg-type]


def replace_entry(**changes: object) -> bytes:
    program = base_program()
    entry = dataclasses.replace(program.functions[0], **changes)  # type: ignore[arg-type]
    return encode(dataclasses.replace(program, functions=(entry,)))


def test_constant_checks() -> None:
    program = base_program()
    check(replace_program(constants=(*program.constants, "x" * 4097)), "longer than 4096")
    check(replace_program(constants=(*program.constants, 2**256)), "outside the 256-bit limit")
    check(replace_program(constants=(*program.constants, dict_of(list_of(INT), INT))), "dict key type")
    check(replace_program(constants=(*program.constants, NONE)), "None is only a function return type")
    check(replace_program(constants=(*program.constants, list_of(NONE))), "None is only")


def test_function_table_checks() -> None:
    check(replace_program(functions=(), code=b""), "defines no functions")
    check(replace_entry(name_index=5), "name must refer to a str constant")
    program = base_program()
    check(encode(dataclasses.replace(program, constants=(7,))), "name must refer to a str constant")
    for bad in ["9lives", "has space", "", chr(0x3042), "x" * 65]:
        check(encode(dataclasses.replace(program, constants=(bad,))), "not an identifier")
    check(encode(dataclasses.replace(program, constants=("__init__",))), "beginning with __")
    entry = program.functions[0]
    twice = dataclasses.replace(program, functions=(entry, dataclasses.replace(entry, code_offset=2)), code=program.code * 2)
    check(encode(twice), "duplicate function name main")


def test_signature_checks() -> None:
    check(replace_entry(params=(INT,) * 17, local_count=17), "more than 16 parameters")
    check(replace_entry(params=(NONE,), local_count=1), "None is only")
    check(replace_entry(params=(list_of(INT),), local_count=1), "public parameter 0 must be")
    check(replace_entry(params=(Type(Kind.VALUE),), local_count=1), "public parameter 0 must be")
    check(replace_entry(returns=Type(Kind.ACCOUNT)), "public return type must be int, str or bool")
    check(replace_entry(returns=NONE), "public return type")
    check(replace_entry(public=False, returns=dict_of(Type(Kind.CALL_RESULT), INT)), "return type: dict key type")
    check(replace_entry(params=(INT, INT), local_count=1), "local count must be between")
    check(replace_entry(local_count=257), "local count must be between")


def test_internal_functions_may_use_any_value_type() -> None:
    text = ok("PUSH_INT 0\nRET", extra=".function helper params=list[int],dict[str,Value],CallResult,Paid returns=list[Quote]\nLOAD 0\nPOP\nBUILD_LIST 0\nRET\n.end\n")
    verify(assemble(text), max_size=MAX)


def test_code_range_checks() -> None:
    check(replace_entry(code_length=0), "code range is empty or outside")
    check(replace_entry(code_length=5), "code range is empty or outside")
    program = base_program()
    check(encode(dataclasses.replace(program, code=program.code + b"\x26")), "code bytes from 3 belong to no function")
    second = FunctionEntry(program.constants.index("main"), False, (), NONE, 0, 1, 2)
    with_helper = dataclasses.replace(program, constants=(*program.constants,), functions=(program.functions[0], second))
    check(encode(with_helper), "overlap|duplicate")
    gap = dataclasses.replace(program, constants=("main", "f"), functions=(program.functions[0], FunctionEntry(1, False, (), NONE, 0, 4, 1)), code=program.code + b"\x00\x26")
    check(encode(gap), "code bytes 3..3 belong to no function")
    overlap = dataclasses.replace(program, constants=("main", "f"), functions=(program.functions[0], FunctionEntry(1, False, (), NONE, 0, 2, 2)), code=program.code + b"\x26")
    check(encode(overlap), "overlap")


def test_undefined_opcode_and_truncated_instruction() -> None:
    asm = Assembler()
    asm.function("main", public=True, returns=INT).raw(b"\xee").emit("PUSH_INT", 0).emit("RET")
    check(asm.assemble(), "undefined opcode 0xee")
    asm = Assembler()
    asm.function("main", public=True, returns=INT).raw(b"\x00").emit("PUSH_INT", 0).emit("RET")
    check(asm.assemble(), "undefined opcode 0x00")
    asm = Assembler()
    asm.function("main", public=True, returns=INT).emit("PUSH_INT", 0).emit("RET").raw(b"\x01\x80")
    check(asm.assemble(), "truncated varint")
    asm = Assembler()
    asm.function("main", public=True, returns=INT).emit("PUSH_INT", 0).raw(b"\x08")
    asm.function("f").emit("RET")
    check(asm.assemble(), "function main at offset 2.*truncated varint")


def test_operand_reference_checks() -> None:
    check(assemble(ok("PUSH_CONST #9\nRET")), "constant 9 does not exist")
    check(assemble(".const type int\n" + ok("PUSH_CONST #0\nRET")), "constant 0 has the wrong kind")
    check(assemble(".const 5\n" + ok("PUSH_TRUE\nASSERT #0\nPUSH_INT 0\nRET")), "constant 0 has the wrong kind")
    check(assemble(ok("PUSH_INT 0\nCHECK_TYPE #0\nRET")), "constant 0 has the wrong kind")
    check(assemble(ok("LOAD 3\nRET", header=".function main public returns=int locals=3")), "local 3 does not exist")
    check(assemble(ok("CALL 4\nRET")), "function 4 does not exist")
    check(assemble(ok("CALL_BUILTIN 999 0\nRET")), "builtin 999 does not exist")
    check(assemble(ok("CALL_BUILTIN balance 1\nRET")), "takes 2 arguments, not 1")
    check(assemble(ok("CALL_BUILTIN storage.get 0\nRET")), "takes 1 to 2 arguments, not 0")
    check(assemble(ok('PUSH_CONST "k"\nPUSH_INT 0\nPUSH_INT 0\nCALL_BUILTIN storage.get 3\nRET')), "takes 1 to 2 arguments, not 3")
    for argc in (1, 2):
        verify(assemble(ok('PUSH_CONST "k"\n' + "PUSH_INT 0\n" * (argc - 1) + f"CALL_BUILTIN storage.get {argc}\nCHECK_TYPE int\nRET")), max_size=MAX)
    check(assemble(ok("PUSH_INT 0\nSLICE 4\nRET")), "undefined flag bits")
    check(assemble(ok("PUSH_INT 0\nLIST_POP 2\nRET")), "undefined flag bits")
    check(assemble(ok("PUSH_INT 0\nPUSH_INT 0\nCALL_CONTRACT 8 0\nRET")), "undefined flag bits")
    check(assemble(ok("PUSH_INT 0\nPUSH_INT 0\nPUSH_INT 0\nCALL_CONTRACT 4 1\nRET")), "must be 0 when the arguments are passed as a list")
    check(assemble(ok("PUSH_INT 0\nPUSH_INT 0\nTRY_CALL 0 17\nRET")), "more than 16 arguments")
    check(assemble(ok("PUSH_INT 0\nGET_FIELD 1 0\nRET")), "1 is not a record kind")
    check(assemble(ok("CALL_BUILTIN paid 0\nGET_FIELD 10 2\nRET")), "has no field 2")
    check(assemble(ok("BUILD_LIST 129\nRET")), "exceeds the stack limit")
    check(assemble(ok("BUILD_DICT 65\nRET")), "exceeds the stack limit")


def test_jump_target_checks() -> None:
    check(assemble(ok("PUSH_INT 300\nJUMP 1\nRET")), "jump target 1 is not an instruction start")
    check(assemble(ok("PUSH_INT 0\nJUMP 50\nRET")), "jump target 50 is not an instruction start")
    across = ok("PUSH_INT 0\nJUMP 5\nRET", extra=".function f returns=int\nPUSH_INT 0\nRET\n.end\n")
    check(assemble(across), "jump target 5 is not an instruction start")
    check(assemble(ok("FOR_RANGE 0 0 0 0 99\nPUSH_INT 0\nRET", header=".function main public returns=int locals=1")), "jump target 99")


def test_stack_checks() -> None:
    check(assemble(ok("ADD\nRET")), "stack underflow")
    check(assemble(ok("PUSH_INT 1\nCALL f\nRET", extra=".function f params=int,int returns=int\nLOAD 0\nRET\n.end\n")), "stack underflow")
    deep = "PUSH_INT 0\n" * 129 + "RET"
    check(assemble(ok(deep)), "stack deeper than 128")
    small = Limits(max_stack_depth=4)
    check(assemble(ok("PUSH_INT 0\n" * 5 + "RET")), "stack deeper than 4", limits=small)
    merge = "PUSH_TRUE\nJUMP_IF_FALSE end\nPUSH_INT 1\nend:\nPUSH_INT 0\nRET"
    check(assemble(ok(merge)), "stack depth 0 on one path and 1 on another|stack depth 1 on one path and 0 on another")
    check(assemble(ok("PUSH_INT 0\nPUSH_INT 0")), "falls off the end")
    check(assemble(ok("PUSH_TRUE\nJUMP_IF_TRUE x\nPUSH_INT 0\nRET\nx:\nPUSH_INT 1\nPOP")), "falls off the end")
    check(assemble(ok("PUSH_INT 0\nPUSH_INT 0\nRET")), "exactly the return value")
    check(assemble(ok("RET")), "stack underflow")
    check(assemble(ok("PUSH_INT 0\nRET", header=".function main returns=None")), "exactly the return value")


def test_unreachable_code_is_allowed_and_loops_are_fine() -> None:
    text = ok("PUSH_INT 0\nRET\nADD\nADD\nRET")
    verify(assemble(text), max_size=MAX)
    loop = ok("loop:\nPUSH_TRUE\nJUMP_IF_TRUE loop\nPUSH_INT 0\nRET")
    verify(assemble(loop), max_size=MAX)
    revert_only = ok('PUSH_CONST "x"\nREVERT')
    verify(assemble(revert_only), max_size=MAX)


def test_jump_into_the_middle_of_an_instruction() -> None:
    asm = Assembler()
    f = asm.function("main", public=True, returns=INT)
    f.emit("PUSH_INT", 1000).emit("JUMP", 1).emit("RET")
    check(asm.assemble(), "jump target 1 is not an instruction start")


def test_predecoded_instructions() -> None:
    asm = Assembler()
    f = asm.function("main", public=True, returns=STR)
    f.emit("PUSH_CONST", "hi").emit("JUMP", "end").label("end").emit("RET")
    verified = verify(asm.assemble(), max_size=MAX)
    code = verified.functions[0].code
    # Free instructions carry minus their free run weight as gas.
    assert code[0][:3] == (int(Op.PUSH_CONST), -2, "hi")
    assert code[1][:3] == (int(Op.JUMP), -1, 2)
    assert code[2][:2] == (int(Op.RET), -3)
    assert [item.offset for item in verified.functions[0].listing] == [0, 2, 4]
    assert ConstRef(0) == ConstRef(0)
