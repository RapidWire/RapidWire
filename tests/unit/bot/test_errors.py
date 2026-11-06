from __future__ import annotations

import logging

import discord
import pytest
from discord import app_commands

from rapidwire.bot.embeds import ERROR_COLOUR
from rapidwire.bot.errors import (
    CALL_FAILED_REASONS,
    COMMAND_OUTDATED_MESSAGE,
    CONTRACT_KINDS,
    DOMAIN_MESSAGES,
    GENERIC_MESSAGE,
    NUMBER_FORMAT_MESSAGES,
    UserInputError,
    error_message,
    present_error,
)
from rapidwire.contract.errors import CallFailure
from rapidwire.core import errors as core_errors
from rapidwire.core.amounts import NumberFormatError, parse_amount
from rapidwire.core.errors import DomainError, error_from_code
from rapidwire.core.ledger import set_failure_output

SECRET = "SELECT * FROM balance WHERE internal_detail=42"


def _domain_error_classes() -> list[type[DomainError]]:
    return [value for value in vars(core_errors).values() if isinstance(value, type) and issubclass(value, DomainError)]


def _codes() -> set[str]:
    return {cls.__dict__["code"] for cls in _domain_error_classes() if "code" in cls.__dict__}


def _text(embed: discord.Embed) -> str:
    return " ".join([embed.title or "", embed.description or ""] + [f"{field.name} {field.value}" for field in embed.fields])


def test_every_core_error_code_has_a_japanese_message() -> None:
    codes = _codes()
    assert codes, "no codes found"
    assert codes - set(DOMAIN_MESSAGES) == set()
    assert set(DOMAIN_MESSAGES) - codes == set()


def test_every_number_format_kind_has_a_message() -> None:
    kinds = set()
    for value in (1.5, "-1", "9" * 60, "1e3", "0"):
        with pytest.raises(NumberFormatError) as caught:
            parse_amount(value, allow_zero=False)
        kinds.add(caught.value.kind)
    assert kinds <= set(NUMBER_FORMAT_MESSAGES)
    assert {"int_string_type", "int_string_format", "int_string_range"} <= set(NUMBER_FORMAT_MESSAGES)


def test_contract_kinds_and_reasons_cover_the_vm() -> None:
    assert {reason.value for reason in CallFailure} == set(CALL_FAILED_REASONS)
    assert {"REVERTED", "RUNTIME", "OUT_OF_GAS", "CALL_FAILED", "MEMORY_LIMIT", "WALL_CLOCK_LIMIT", "HOST_TIME_LIMIT", "SETTLEMENT"} == set(CONTRACT_KINDS)


@pytest.mark.parametrize("cls", _domain_error_classes(), ids=lambda cls: cls.__name__)
def test_domain_errors_never_show_their_message(cls: type[DomainError]) -> None:
    error = cls(SECRET, execution_id=77)
    embed = present_error(error)
    assert embed.title == "エラー"
    assert embed.colour == ERROR_COLOUR
    if error.code in {"contract_reverted", "invalid_contract"}:
        assert any(field.name == "詳細" for field in embed.fields)
    else:
        assert SECRET not in _text(embed)
    assert embed.description == DOMAIN_MESSAGES.get(error.code, DOMAIN_MESSAGES["domain_error"])
    assert [field.value for field in embed.fields if field.name == "実行ID"] == ["`77`"]


def test_subclass_without_its_own_code_uses_the_base_message() -> None:
    class Custom(core_errors.InsufficientBalance):
        pass

    assert error_message(Custom()) == DOMAIN_MESSAGES["insufficient_balance"]


def test_unknown_stored_code_falls_back_to_the_generic_domain_message() -> None:
    error = error_from_code("some_future_code", SECRET)
    assert error_message(error) == DOMAIN_MESSAGES["domain_error"]
    assert SECRET not in _text(present_error(error))


def test_replayed_error_rebuilt_from_its_code_maps_like_the_original() -> None:
    error = error_from_code("slippage_exceeded", SECRET, execution_id=5)
    embed = present_error(error)
    assert embed.description == DOMAIN_MESSAGES["slippage_exceeded"]
    assert SECRET not in _text(embed)


def test_overrides_replace_the_message_by_code() -> None:
    error = core_errors.InsufficientBalance(SECRET)
    assert error_message(error, overrides={"insufficient_balance": "残高か承認額が不足しています。"}) == "残高か承認額が不足しています。"
    assert error_message(core_errors.InsufficientAllowance(), overrides={"insufficient_balance": "x"}) == DOMAIN_MESSAGES["insufficient_allowance"]


def test_contract_failure_fields() -> None:
    error = core_errors.ContractCallFailed(SECRET, execution_id=9)
    set_failure_output(error, {"kind": "CALL_FAILED", "reason": "NO_FUNCTION", "gas_used": 12, "gas_limit": "128", "location": SECRET, "host_code": SECRET})
    error.child = {"type": "contract_call", "contract_function": "f", "code": "insufficient_balance", "message": SECRET}
    embed = present_error(error)
    fields = {field.name: field.value for field in embed.fields}
    assert fields["種別"] == f"{CONTRACT_KINDS['CALL_FAILED']} (`CALL_FAILED`)"
    assert fields["理由"] == CALL_FAILED_REASONS["NO_FUNCTION"]
    assert fields["使用ガス"] == "`12 / 128`"
    assert fields["原因"] == DOMAIN_MESSAGES["insufficient_balance"]
    assert fields["実行ID"] == "`9`"
    assert "ガス代" not in fields
    assert SECRET not in _text(embed)


def test_contract_failure_gas_with_separators_and_cost() -> None:
    error = core_errors.ContractOutOfGas(SECRET)
    set_failure_output(error, {"kind": "OUT_OF_GAS", "gas_used": "150000", "gas_limit": 150_000, "gas_currency_id": 1, "gas_price": "3"})
    fields = {field.name: field.value for field in present_error(error, gas_symbol="RAPID").fields}
    assert fields["使用ガス"] == "`150,000 / 150,000`"
    assert fields["ガス代"] == "`450.000 RAPID`"
    assert {field.name: field.value for field in present_error(error).fields}["ガス代"] == "`450.000`"


def test_unknown_contract_kind_is_not_shown() -> None:
    error = core_errors.ContractRuntimeFailure(SECRET)
    set_failure_output(error, {"kind": SECRET, "reason": SECRET, "gas_used": SECRET, "gas_limit": True})
    embed = present_error(error)
    assert embed.fields == []


def test_revert_text_is_shown_in_a_code_block() -> None:
    error = core_errors.ContractReverted("Send at least 100 RAPID ```@everyone", execution_id=3)
    fields = {field.name: field.value for field in present_error(error).fields}
    assert fields["詳細"] == "```Send at least 100 RAPID '''@everyone```"


def test_user_input_and_number_format_errors() -> None:
    assert present_error(UserInputError("ページ番号が正しくありません。")).description == "ページ番号が正しくありません。"
    with pytest.raises(NumberFormatError) as caught:
        parse_amount("1.2345", allow_zero=False)
    embed = present_error(caught.value)
    assert embed.description == NUMBER_FORMAT_MESSAGES["amount_format"]
    assert caught.value.message not in _text(embed)


def test_app_command_errors() -> None:
    assert error_message(app_commands.NoPrivateMessage()) == "このコマンドはサーバー内でのみ使用できます。"
    assert error_message(app_commands.MissingPermissions(["administrator"])) == "この操作を行う権限がありません。"


def _outdated_errors() -> list[app_commands.AppCommandError]:
    class Dummy:
        name = "dummy"
        qualified_name = "dummy"

    return [app_commands.CommandNotFound("pay", ["currency"]), app_commands.CommandSignatureMismatch(Dummy())]  # type: ignore[arg-type]


@pytest.mark.parametrize("error", _outdated_errors(), ids=lambda error: type(error).__name__)
def test_outdated_command_is_a_warning_without_a_stack_trace(caplog: pytest.LogCaptureFixture, error: app_commands.AppCommandError) -> None:
    caplog.set_level(logging.WARNING, logger="rapidwire.bot")
    assert error_message(error) == COMMAND_OUTDATED_MESSAGE == "コマンドが更新されました。しばらくしてから再度お試しください。"
    embed = present_error(error, log_extra={"interaction_id": 5})
    assert embed.description == COMMAND_OUTDATED_MESSAGE
    [record] = [record for record in caplog.records if record.name == "rapidwire.bot"]
    assert (record.levelno, record.getMessage(), record.exc_info) == (logging.WARNING, "an outdated application command was invoked", None)
    assert (record.error, record.interaction_id) == (type(error).__name__, 5)  # type: ignore[attr-defined]


def test_expected_errors_are_not_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="rapidwire.bot")
    present_error(core_errors.InsufficientBalance())
    present_error(app_commands.CheckFailure())
    assert [record for record in caplog.records if record.name == "rapidwire.bot"] == []


def test_invoke_error_is_unwrapped() -> None:
    class Dummy:
        name = "dummy"

    wrapped = app_commands.CommandInvokeError(Dummy(), core_errors.NoRoute(SECRET))  # type: ignore[arg-type]
    assert error_message(wrapped) == DOMAIN_MESSAGES["no_route"]


def test_unexpected_exception_is_generic_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.ERROR, logger="rapidwire.bot")
    try:
        raise RuntimeError(SECRET)
    except RuntimeError as error:
        embed = present_error(error)
    assert embed.description == GENERIC_MESSAGE
    assert SECRET not in _text(embed)
    [record] = [record for record in caplog.records if record.name == "rapidwire.bot"]
    assert record.exc_info is not None
    assert record.exc_info[1] is not None and SECRET in str(record.exc_info[1])
