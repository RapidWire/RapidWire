"""The public function list of the /contract embeds, cut to one embed field."""

from __future__ import annotations

from rapidwire.bot.commands.contract import _functions_text
from rapidwire.bot.embeds import FIELD_VALUE_MAX
from rapidwire.contract.types import INT
from rapidwire.core.contracts import PublicFunction


def test_functions_text_lists_every_signature_that_fits() -> None:
    assert _functions_text([]) == "なし"
    functions = [PublicFunction("a", (), INT), PublicFunction("b", (INT,), INT)]
    assert _functions_text(functions) == "`a() -> int`\n`b(int) -> int`"


def test_functions_text_counts_the_functions_left_out() -> None:
    functions = [PublicFunction(f"f{index:03d}" + "x" * 60, (INT, INT), INT) for index in range(100)]
    text = _functions_text(functions)
    lines = text.splitlines()
    assert len(text) <= FIELD_VALUE_MAX
    shown = len(lines) - 1
    assert 0 < shown < len(functions)
    assert lines[:shown] == [f"`{function.signature}`" for function in functions[:shown]]
    assert lines[-1] == f"... 他 {len(functions) - shown} 件"
