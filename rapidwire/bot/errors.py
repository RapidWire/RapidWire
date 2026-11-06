"""The shared error presenter: every failure a handler raises becomes a Japanese error embed built from the error's code and safe structured fields, never from exception text, and unexpected exceptions are logged with their stack trace."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Final

import discord
from discord import app_commands

from rapidwire.bot.embeds import Field, amount_text, error_embed
from rapidwire.core import errors as core_errors
from rapidwire.core.amounts import DECIMALS, MINIMAL_UNIT, NumberFormatError
from rapidwire.core.errors import DomainError
from rapidwire.core.ledger import failure_output

__all__ = [
    "COMMAND_OUTDATED_MESSAGE",
    "DOMAIN_MESSAGES",
    "GENERIC_MESSAGE",
    "NUMBER_FORMAT_MESSAGES",
    "UserInputError",
    "error_message",
    "present_error",
]

log = logging.getLogger("rapidwire.bot")


class UserInputError(Exception):
    """An argument a handler could not accept; message is Japanese text written by the bot and is shown to the user as is."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


GENERIC_MESSAGE: Final = "予期せぬエラーが発生しました。時間をおいて再度お試しください。"

# Messages by DomainError code; an error whose code is absent uses the entry of its nearest base class.
DOMAIN_MESSAGES: Final[Mapping[str, str]] = {
    core_errors.DomainError.code: "操作を完了できませんでした。",
    core_errors.NotFoundError.code: "対象が見つかりませんでした。",
    core_errors.CurrencyNotFound.code: "対象の通貨が見つかりませんでした。",
    core_errors.ClaimNotFound.code: "請求が見つかりませんでした。",
    core_errors.AccountNotFound.code: "アカウントが見つかりませんでした。",
    core_errors.InsufficientBalance.code: "残高が不足しています。",
    core_errors.InsufficientAllowance.code: "承認額 (Allowance) が不足しています。",
    core_errors.InsufficientStake.code: "ステーキングしている額が不足しています。",
    core_errors.InvalidAmount.code: f"金額が正しくありません。{MINIMAL_UNIT} 以上の金額を指定してください。",
    core_errors.InvalidArgument.code: "指定された値が正しくありません。",
    core_errors.PermissionDenied.code: "この操作を行う権限がありません。",
    core_errors.Unauthorized.code: "有効なAPIトークンが必要です。",
    core_errors.GuildUnavailable.code: "Botがこのサーバーに参加していないため、サーバーの所有者と管理者を確認できません。",
    core_errors.CurrencyRenounced.code: "この通貨はMintと利率変更が放棄されています。",
    core_errors.TimelockActive.code: "待機期間がまだ終わっていません。",
    core_errors.RequestExpired.code: "要求の有効期限が切れています。もう一度要求してください。",
    core_errors.NoPendingRequest.code: "保留中の要求はありません。",
    core_errors.AlreadyExists.code: "既に存在します。",
    core_errors.InvalidState.code: "現在の状態ではこの操作を行えません。",
    core_errors.AmountOverflow.code: "結果の金額が扱える範囲を超えます。",
    core_errors.IdempotencyConflict.code: "この操作は別の内容で既に処理されています。",
    core_errors.PoolNotFound.code: "流動性プールが見つかりませんでした。",
    core_errors.PoolAlreadyExists.code: "この通貨ペアと手数料の段階の流動性プールは既に存在します。",
    core_errors.SlippageExceeded.code: "価格が変動したため、受取額が許容する最小額を下回りました。再度お試しください。",
    core_errors.InsufficientLiquidity.code: "流動性プールの流動性が不足しています。",
    core_errors.InsufficientShares.code: "保有しているシェアが不足しています。",
    core_errors.NoRoute.code: "スワップルートが見つかりませんでした。",
    core_errors.ContractNotFound.code: "このアカウントにはコントラクトが登録されていません。",
    core_errors.ContractLocked.code: "コントラクトはロック中のため、ロック期限まで更新・一時停止・削除できません。",
    core_errors.ContractPaused.code: "コントラクトは一時停止中です。",
    core_errors.InvalidContract.code: "コントラクトのバイナリが正しくありません。",
    core_errors.ContractStorageLimit.code: "コントラクトのストレージの上限を超えます。",
    core_errors.RevertError.code: "操作は取り消されました。",
    core_errors.ContractReverted.code: "取引はコントラクトによってキャンセルされました。",
    core_errors.ContractRuntimeFailure.code: "コントラクトの実行中にエラーが発生しました。",
    core_errors.ContractOutOfGas.code: "コントラクトの実行中にガスが不足しました。",
    core_errors.ContractCallFailed.code: "コントラクトの関数を呼び出せませんでした。",
    core_errors.ContractLimitExceeded.code: "コントラクトが実行の上限 (メモリまたは時間) を超えました。",
}

_AMOUNT_FORMAT = f"金額は小数点以下 {DECIMALS} 桁までの数値で指定してください (例: 12.5)。符号、指数表記、空白、先頭の余分な 0 は使えません。"
_INT_FORMAT = "整数で指定してください (例: 123)。+ の符号、空白、先頭の余分な 0 は使えません。"

# Messages by NumberFormatError kind.
NUMBER_FORMAT_MESSAGES: Final[Mapping[str, str]] = {
    "amount_type": _AMOUNT_FORMAT,
    "amount_format": _AMOUNT_FORMAT,
    "amount_negative": "金額に負の値は指定できません。",
    "amount_range": "金額が扱える範囲を超えています。",
    "amount_zero": f"金額は {MINIMAL_UNIT} 以上を指定してください。",
    "int_string_type": _INT_FORMAT,
    "int_string_format": _INT_FORMAT,
    "int_string_range": "値が指定できる範囲を超えています。",
}

# Labels of the failure kinds recorded on failed contract executions.
CONTRACT_KINDS: Final[Mapping[str, str]] = {
    "REVERTED": "取り消し",
    "RUNTIME": "実行時エラー",
    "OUT_OF_GAS": "ガス不足",
    "CALL_FAILED": "呼び出しの失敗",
    "MEMORY_LIMIT": "メモリの上限超過",
    "WALL_CLOCK_LIMIT": "実行時間の上限超過",
    "HOST_TIME_LIMIT": "処理時間の上限超過",
    "SETTLEMENT": "ガス代の精算の失敗",
}

# Labels of the CALL_FAILED reasons.
CALL_FAILED_REASONS: Final[Mapping[str, str]] = {
    "NO_CONTRACT": "呼び出し先にコントラクトがありません",
    "INACTIVE": "呼び出し先のコントラクトが停止中です",
    "NO_FUNCTION": "その名前の公開関数がありません",
    "BAD_ARGUMENTS": "引数の数または型が合いません",
    "INSUFFICIENT_BALANCE": "ガス代の前払いまたは添付する額の残高が不足しています",
    "DEPTH": "呼び出しの深さが上限に達しました",
    "REENTRANCY": "実行中のコントラクトを再び呼び出せません",
    "NO_CURRENCY": "通貨が見つかりません",
}

# Codes whose message is meant for the user: a contract's revert text and the verifier's description of a rejected binary.
_DETAIL_CODES: Final = frozenset({core_errors.ContractReverted.code, core_errors.InvalidContract.code})
_DETAIL_MAX: Final = 900

COMMAND_OUTDATED_MESSAGE: Final = "コマンドが更新されました。しばらくしてから再度お試しください。"

# Errors raised when a client invokes a command from before the last tree sync; they are expected during a deployment and logged without a stack trace.
_OUTDATED_COMMAND_ERRORS: Final = (app_commands.CommandNotFound, app_commands.CommandSignatureMismatch)

_APP_COMMAND_MESSAGES: Final[tuple[tuple[type[app_commands.AppCommandError], str], ...]] = (
    (app_commands.CommandNotFound, COMMAND_OUTDATED_MESSAGE),
    (app_commands.CommandSignatureMismatch, COMMAND_OUTDATED_MESSAGE),
    (app_commands.NoPrivateMessage, "このコマンドはサーバー内でのみ使用できます。"),
    (app_commands.MissingPermissions, "この操作を行う権限がありません。"),
    (app_commands.BotMissingPermissions, "Botにこの操作を行う権限がありません。"),
    (app_commands.CommandOnCooldown, "しばらく待ってから再度お試しください。"),
    (app_commands.CheckFailure, "このコマンドを実行できません。"),
    (app_commands.TransformerError, "引数の値が正しくありません。"),
)


def _domain_message(error: DomainError, overrides: Mapping[str, str]) -> str:
    for cls in type(error).__mro__:
        code = cls.__dict__.get("code") if cls is not type(error) else error.code
        if not isinstance(code, str):
            continue
        if code in overrides:
            return overrides[code]
        if code in DOMAIN_MESSAGES:
            return DOMAIN_MESSAGES[code]
    return DOMAIN_MESSAGES[DomainError.code]


def _unwrap(error: BaseException) -> BaseException:
    if isinstance(error, app_commands.CommandInvokeError):
        return error.original
    return error


def error_message(error: BaseException, *, overrides: Mapping[str, str] | None = None) -> str | None:
    """The Japanese message for an expected failure (DomainError by code, then its base classes; NumberFormatError by kind; UserInputError; app command check and argument failures), or None for an unexpected exception; overrides replaces the message of DomainError codes."""
    error = _unwrap(error)
    if isinstance(error, UserInputError):
        return error.message
    if isinstance(error, DomainError):
        return _domain_message(error, overrides or {})
    if isinstance(error, NumberFormatError):
        return NUMBER_FORMAT_MESSAGES.get(error.kind, _AMOUNT_FORMAT)
    for cls, message in _APP_COMMAND_MESSAGES:
        if isinstance(error, cls):
            return message
    return None


def _number_text(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return value
    return None


def _code_block(text: str) -> str:
    # Inside a code block nothing is rendered or mentioned, so only backticks need replacing.
    body = text.replace("`", "'")
    if len(body) > _DETAIL_MAX:
        body = body[: _DETAIL_MAX - 3] + "..."
    return f"```{body}```"


def _domain_fields(error: DomainError, gas_symbol: str | None) -> list[Field]:
    fields: list[Field] = []
    output = failure_output(error)
    kind = output.get("kind")
    if isinstance(kind, str) and kind in CONTRACT_KINDS:
        fields.append(Field("種別", f"{CONTRACT_KINDS[kind]} (`{kind}`)", inline=True))
    reason = output.get("reason")
    if isinstance(reason, str) and reason in CALL_FAILED_REASONS:
        fields.append(Field("理由", CALL_FAILED_REASONS[reason], inline=True))
    gas_used, gas_limit = _number_text(output.get("gas_used")), _number_text(output.get("gas_limit"))
    if gas_used is not None and gas_limit is not None:
        fields.append(Field("使用ガス", f"`{int(gas_used):,} / {int(gas_limit):,}`", inline=True))
        gas_price = _number_text(output.get("gas_price"))
        if gas_price is not None:
            fields.append(Field("ガス代", amount_text(int(gas_used) * int(gas_price), gas_symbol), inline=True))
    child = error.child
    if child is not None:
        child_code = child.get("code")
        if isinstance(child_code, str) and child_code in DOMAIN_MESSAGES:
            fields.append(Field("原因", DOMAIN_MESSAGES[child_code]))
    if error.code in _DETAIL_CODES and error.message and error.message != type(error).default_message:
        fields.append(Field("詳細", _code_block(error.message)))
    if error.execution_id is not None:
        fields.append(Field("実行ID", f"`{error.execution_id}`"))
    return fields


def present_error(error: BaseException, *, overrides: Mapping[str, str] | None = None, log_extra: Mapping[str, Any] | None = None, gas_symbol: str | None = None) -> discord.Embed:
    """The error embed shown for error: the message of error_message() with, for a DomainError, the contract failure kind and reason, gas used and its cost (followed by gas_symbol when given), the failed child's cause, the contract's revert text or the verifier's description, and the execution ID field of the recorded execution.
    An unexpected exception gets GENERIC_MESSAGE and is logged with its stack trace, an outdated command is logged as a warning without one, and log_extra (such as interaction_log_extra()) is added to those records.
    """
    message = error_message(error, overrides=overrides)
    original = _unwrap(error)
    extra = dict(log_extra or {})
    if message is None:
        log.error("unexpected error in a bot handler", exc_info=(type(original), original, original.__traceback__), extra=extra)
        return error_embed(GENERIC_MESSAGE)
    if isinstance(original, _OUTDATED_COMMAND_ERRORS):
        log.warning("an outdated application command was invoked", extra={**extra, "error": type(original).__name__})
    if isinstance(original, DomainError):
        return error_embed(message, fields=_domain_fields(original, gas_symbol))
    return error_embed(message)
