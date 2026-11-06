"""The /contract group: set, get, info, call, lock, pause, resume and delete."""

from __future__ import annotations

import io
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Final

import discord
from discord import app_commands

from rapidwire.bot.autocomplete import symbol_autocomplete
from rapidwire.bot.client import RapidWireBot, bot_context
from rapidwire.bot.context import BotContext
from rapidwire.bot.contract_args import args_autocomplete, function_autocomplete, parse_call_args, symbol_resolver
from rapidwire.bot.embeds import FIELD_VALUE_MAX, Field, amount_text, info_embed, success_embed, timestamp_full, timestamp_relative
from rapidwire.bot.errors import UserInputError, error_message, present_error
from rapidwire.bot.interaction import Reply, idempotency_key, interaction_log_extra, run_command
from rapidwire.bot.symbols import resolve_currency
from rapidwire.config import MAX_GAS_LIMIT
from rapidwire.contract.values import Attachment, Currency
from rapidwire.core.amounts import parse_amount
from rapidwire.core.contract_calls import ContractCallResult, call_contract
from rapidwire.core.contracts import (
    CONTRACT_STORAGE_MAX_BYTES,
    CONTRACT_STORAGE_MAX_KEYS,
    LOCK_HOURS_MAX,
    PublicFunction,
    delete_contract,
    get_contract,
    get_contract_with_functions,
    is_locked,
    lock_contract,
    pause_contract,
    public_functions,
    register_contract,
    resume_contract,
)
from rapidwire.core.currency import get_currency
from rapidwire.core.errors import ContractLocked, ContractNotFound, DomainError, InvalidState
from rapidwire.db.tables import ContractStatus

__all__ = ["ContractGroup", "setup"]

log = logging.getLogger("rapidwire.bot")

STATUS_LABELS: Final = {ContractStatus.ACTIVE: "有効", ContractStatus.PAUSED: "一時停止中", ContractStatus.DELETED: "削除済み"}

# The suffix of the idempotency key of the lock that /contract set applies after registering, which is a second operation of the same interaction.
LOCK_KEY_SUFFIX: Final = ":lock"

_LOCK_PAUSED: Final = "一時停止中のコントラクトはロックできません。先に再開してください。"
_LOCK_EXTEND_ONLY: Final = "ロック期限は延長のみできます。現在のロック期限より後になる期間を指定してください。"
_LOCK_UNEXPECTED: Final = "予期せぬエラーが発生しました。`/contract lock` で再度お試しください。"
_PAUSE_OVERRIDES: Final = {InvalidState.code: "コントラクトは既に一時停止中です。"}
_RESUME_OVERRIDES: Final = {InvalidState.code: "コントラクトは一時停止中ではありません。", ContractLocked.code: "コントラクトはロック中のため、ロック期限まで状態を変更できません。"}
_VALUE_MAX: Final = 1000


def _now(ctx: BotContext) -> datetime:
    return ctx.now() or datetime.now(UTC)


def _lock_text(locked_until: datetime | None, now: datetime) -> str:
    if locked_until is None or not is_locked(locked_until, now):
        return "ロックされていません"
    return f"{timestamp_full(locked_until)} ({timestamp_relative(locked_until)})"


def _functions_text(functions: Sequence[PublicFunction]) -> str:
    """One `signature` per line, cut to fit an embed field with the number of functions left out."""
    if not functions:
        return "なし"
    lines: list[str] = []
    used = 0
    for index, function in enumerate(functions):
        line = f"`{function.signature}`"
        rest = len(functions) - index
        tail = len(f"\n... 他 {rest} 件")
        if used + len(line) + 1 + (tail if rest > 1 else 0) > FIELD_VALUE_MAX:
            lines.append(f"... 他 {rest} 件")
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


def _code_block(text: str) -> str:
    if not text:
        return "(空の文字列)"
    body = text.replace("`", "'")
    if len(body) > _VALUE_MAX:
        body = body[: _VALUE_MAX - 3] + "..."
    return f"```{body}```"


def _value_field(value: int | str | bool) -> Field:
    if isinstance(value, bool):
        return Field("戻り値 (`bool`)", "`true`" if value else "`false`")
    if isinstance(value, int):
        return Field("戻り値 (`int`)", f"`{value}`")
    return Field("戻り値 (`str`)", _code_block(value))


def _not_set(target: discord.abc.User, invoker_id: int) -> Reply:
    message = "現在、コントラクトは設定されていません。" if target.id == invoker_id else f"{target.mention} はコントラクトを設定していません。"
    return Reply(embed=success_embed("コントラクト情報", message))


async def _lock_overrides(ctx: BotContext, account_id: int, error: DomainError) -> dict[str, str]:
    """The overrides for a failed lock: InvalidState means a paused contract or a lock that would not be extended, told apart by reading the contract again."""
    if not isinstance(error, InvalidState):
        return {}
    info = await get_contract(ctx.engine, account_id)
    if info is not None and info.status is ContractStatus.PAUSED:
        return {InvalidState.code: _LOCK_PAUSED}
    if info is not None and info.locked_until is not None and is_locked(info.locked_until, _now(ctx)):
        return {InvalidState.code: f"ロック期限は延長のみできます。現在のロック期限 ({timestamp_full(info.locked_until)}) より後になる期間を指定してください。"}
    return {InvalidState.code: _LOCK_EXTEND_ONLY}


async def _set_lock_failure_text(ctx: BotContext, interaction: discord.Interaction[Any], error: Exception) -> str:
    """Why the lock after a registration failed, for the success embed; an unexpected exception, also while reading the contract for the message, is logged and shown as _LOCK_UNEXPECTED."""
    if not isinstance(error, DomainError):
        log.error("the lock after a contract registration failed", exc_info=error, extra=interaction_log_extra(interaction))
        return _LOCK_UNEXPECTED
    try:
        overrides = await _lock_overrides(ctx, interaction.user.id, error)
    except Exception:
        log.exception("reading the contract after a failed lock failed", extra=interaction_log_extra(interaction))
        overrides = {}
    return error_message(error, overrides=overrides) or _LOCK_UNEXPECTED


def _function_name_text(name: str) -> str:
    shown = name.replace("`", "'")
    return shown if len(shown) <= 64 else shown[:61] + "..."


class ContractGroup(app_commands.Group, name="contract", description="コントラクトの登録・管理・呼び出しを行います。"):
    """The /contract commands; the management commands act on the invoker's own contract."""

    @app_commands.command(name="set", description="あなたのアカウントにコントラクトを設定します。")
    @app_commands.describe(binary="コンパイルしたコントラクトのバイナリファイル (.rwc)", lock_hours="設定後にコントラクトの更新を禁止する期間 (時間単位)。省略するとロックしません")
    async def set_contract(self, interaction: discord.Interaction[Any], binary: discord.Attachment, lock_hours: app_commands.Range[int, 1, LOCK_HOURS_MAX] | None = None) -> None:
        """Register the attached binary as the next version, refusing a declared size outside 1 to contract_max_size before downloading it, then lock for lock_hours when given; a lock that fails is reported beside the registration."""

        async def work() -> Reply:
            ctx = bot_context(interaction)
            max_size = ctx.settings.contract_max_size
            if binary.size < 1:
                raise UserInputError("添付ファイルが空です。")
            if binary.size > max_size:
                raise UserInputError(f"コントラクトのバイナリは {max_size:,} バイト以下にしてください (添付ファイルは {binary.size:,} バイトです)。")
            try:
                data = await binary.read()
            except discord.HTTPException:
                raise UserInputError("添付ファイルをダウンロードできませんでした。もう一度お試しください。") from None
            key = idempotency_key(interaction)
            account_id = interaction.user.id
            registered = await register_contract(ctx.engine, account_id=account_id, binary=data, max_size=max_size, now=ctx.now(), idempotency_key=key)
            version_text = f"`v{registered.version}`" + (" (削除済みのコントラクトを復元しました)" if registered.revived else "")
            fields = [
                Field("版", version_text, inline=True),
                Field("状態", STATUS_LABELS[registered.status], inline=True),
                Field("サイズ", f"`{registered.size:,} バイト`", inline=True),
                Field("SHA-256", f"`{registered.sha256}`"),
            ]
            if lock_hours is not None:
                try:
                    locked = await lock_contract(ctx.engine, account_id=account_id, hours=lock_hours, now=ctx.now(), idempotency_key=key + LOCK_KEY_SUFFIX)
                except Exception as error:
                    fields.append(Field("ロック期限", f"ロックできませんでした: {await _set_lock_failure_text(ctx, interaction, error)}"))
                else:
                    fields.append(Field("ロック期限", _lock_text(locked.locked_until, _now(ctx))))
            fields.append(Field("公開関数", _functions_text(public_functions(data))))
            fields.append(Field("実行ID", f"`{registered.execution_id}`"))
            return Reply(embed=success_embed(description="コントラクトを正常に設定しました。", fields=fields))

        await run_command(interaction, work)

    @app_commands.command(name="get", description="現在設定されているコントラクトを取得します。")
    @app_commands.describe(user="コントラクトを取得するユーザー (省略時は自分)")
    async def get(self, interaction: discord.Interaction[Any], user: discord.User | None = None) -> None:
        """The current version's binary as a file with its version, SHA-256, size, status, lock and public functions."""

        async def work() -> Reply:
            ctx = bot_context(interaction)
            target: discord.abc.User = user or interaction.user
            details = await get_contract_with_functions(ctx.engine, target.id)
            if details is None or details.binary is None:
                return _not_set(target, interaction.user.id)
            info = details.info
            fields = [
                Field("ユーザー", target.mention),
                Field("版", f"`v{info.current_version}`", inline=True),
                Field("状態", STATUS_LABELS[info.status], inline=True),
                Field("サイズ", f"`{info.size:,} バイト`", inline=True),
                Field("SHA-256", f"`{info.sha256}`"),
                Field("ロック期限", _lock_text(info.locked_until, _now(ctx))),
                Field("公開関数", _functions_text(details.functions)),
            ]
            file = discord.File(io.BytesIO(details.binary), filename=f"contract-{target.id}-v{info.current_version}.rwc")
            return Reply(embed=success_embed("コントラクト詳細", fields=fields), files=(file,))

        await run_command(interaction, work)

    @app_commands.command(name="info", description="コントラクトの状態を表示します。")
    @app_commands.describe(user="コントラクトを表示するユーザー (省略時は自分)")
    async def info(self, interaction: discord.Interaction[Any], user: discord.User | None = None) -> None:
        """Status, current version and version count, SHA-256, lock, storage counters, public functions and the last update."""

        async def work() -> Reply:
            ctx = bot_context(interaction)
            target: discord.abc.User = user or interaction.user
            details = await get_contract_with_functions(ctx.engine, target.id)
            if details is None:
                return _not_set(target, interaction.user.id)
            info = details.info
            fields = [
                Field("ユーザー", target.mention),
                Field("状態", STATUS_LABELS[info.status], inline=True),
                Field("現在の版", f"`v{info.current_version}` (全 {info.version_count} 版)", inline=True),
                Field("サイズ", f"`{info.size:,} バイト`", inline=True),
                Field("SHA-256", f"`{info.sha256}`"),
                Field("ロック期限", _lock_text(info.locked_until, _now(ctx))),
                Field("ストレージ", f"`{info.storage_keys:,} / {CONTRACT_STORAGE_MAX_KEYS:,}` キー、`{info.storage_bytes:,} / {CONTRACT_STORAGE_MAX_BYTES:,}` バイト"),
                Field("公開関数", _functions_text(details.functions)),
                Field("最終更新", f"{timestamp_full(info.updated_at)} (実行ID `{info.updated_execution_id}`)"),
            ]
            return Reply(embed=info_embed("コントラクト情報", fields=fields))

        await run_command(interaction, work)

    @app_commands.command(name="call", description="指定したユーザーのコントラクトの関数を実行します。")
    @app_commands.describe(
        user="コントラクトの所有者",
        function="呼び出す関数",
        args='関数に渡す引数。カンマ区切りで指定します (例: 100, "text", <@ユーザーID>, RAPID)',
        pay="コントラクトに支払う額",
        currency="支払う通貨のシンボル (省略時はこのサーバーの通貨)",
        gas="ガスの上限 (省略時は既定値)",
    )
    @app_commands.autocomplete(function=function_autocomplete, args=args_autocomplete, currency=symbol_autocomplete)
    async def call(
        self,
        interaction: discord.Interaction[Any],
        user: discord.User,
        function: str,
        args: str | None = None,
        pay: str | None = None,
        currency: str | None = None,
        gas: app_commands.Range[int, 1, MAX_GAS_LIMIT] | None = None,
    ) -> None:
        """Call a public function of user's contract with args parsed by its signature, pay attached in currency, and gas as the gas limit."""

        async def work() -> Reply:
            ctx = bot_context(interaction)
            caller_id = interaction.user.id
            details = await get_contract_with_functions(ctx.engine, user.id)
            if details is None or details.info.status is ContractStatus.DELETED:
                raise ContractNotFound()
            name = function.strip()
            target = next((item for item in details.functions if item.name == name), None)
            if target is None:
                raise UserInputError(f"関数 `{_function_name_text(name)}` はこのコントラクトの公開関数ではありません。")
            max_gas = ctx.runtime_config.max_gas
            if gas is not None and gas > max_gas:
                raise UserInputError(f"ガスの上限は 1 から {max_gas:,} の範囲で指定してください。")
            attach: Attachment | None = None
            symbol: str | None = None
            if pay is None:
                if currency is not None and currency.strip():
                    raise UserInputError("currency は pay と一緒に指定してください。")
            else:
                units = parse_amount(pay, allow_zero=False)
                if user.id == caller_id:
                    raise UserInputError("自分のコントラクトを呼び出すときは支払いを添付できません。")
                paid_currency = await resolve_currency(ctx, interaction, currency)
                attach, symbol = Attachment(Currency(paid_currency.currency_id), units), paid_currency.symbol
            values = await parse_call_args(args, target, resolve_currency=symbol_resolver(ctx.engine, now=ctx.now()), limits=ctx.runtime_config.limits)
            try:
                result = await call_contract(
                    ctx.engine,
                    ctx.guild_info,
                    caller_id=caller_id,
                    contract_id=user.id,
                    function=target.name,
                    args=values,
                    attach=attach,
                    gas_limit=gas,
                    config=ctx.runtime_config,
                    now=ctx.now(),
                    idempotency_key=idempotency_key(interaction),
                )
            except DomainError as error:
                return Reply(embed=present_error(error, gas_symbol=await _gas_symbol(ctx, ctx.runtime_config.gas_currency_id)))
            return Reply(embed=await _call_embed(ctx, user, target, result, attach, symbol))

        await run_command(interaction, work)

    @app_commands.command(name="lock", description="あなたのコントラクトの更新・一時停止・削除を指定した期間禁止します。")
    @app_commands.describe(hours="ロックする期間 (時間単位)。ロックは延長のみできます")
    async def lock(self, interaction: discord.Interaction[Any], hours: app_commands.Range[int, 1, LOCK_HOURS_MAX]) -> None:
        """Lock the invoker's contract until hours from now; an existing lock can only be extended."""

        async def work() -> Reply:
            ctx = bot_context(interaction)
            try:
                result = await lock_contract(ctx.engine, account_id=interaction.user.id, hours=hours, now=ctx.now(), idempotency_key=idempotency_key(interaction))
            except DomainError as error:
                return Reply(embed=present_error(error, overrides=await _lock_overrides(ctx, interaction.user.id, error)))
            fields = [Field("ロック期限", _lock_text(result.locked_until, _now(ctx)))]
            if result.previous_locked_until is not None:
                fields.append(Field("以前のロック期限", timestamp_full(result.previous_locked_until)))
            fields.append(Field("実行ID", f"`{result.execution_id}`"))
            return Reply(embed=success_embed(description="コントラクトをロックしました。", fields=fields))

        await run_command(interaction, work)

    @app_commands.command(name="pause", description="あなたのコントラクトを一時停止します。")
    async def pause(self, interaction: discord.Interaction[Any]) -> None:
        """Pause the invoker's active, unlocked contract."""

        async def work() -> Reply:
            ctx = bot_context(interaction)
            result = await pause_contract(ctx.engine, account_id=interaction.user.id, now=ctx.now(), idempotency_key=idempotency_key(interaction))
            return Reply(embed=success_embed(description="コントラクトを一時停止しました。", fields=[Field("実行ID", f"`{result.execution_id}`")]))

        await run_command(interaction, work, overrides=_PAUSE_OVERRIDES)

    @app_commands.command(name="resume", description="一時停止中のあなたのコントラクトを再開します。")
    async def resume(self, interaction: discord.Interaction[Any]) -> None:
        """Resume the invoker's paused, unlocked contract."""

        async def work() -> Reply:
            ctx = bot_context(interaction)
            result = await resume_contract(ctx.engine, account_id=interaction.user.id, now=ctx.now(), idempotency_key=idempotency_key(interaction))
            return Reply(embed=success_embed(description="コントラクトを再開しました。", fields=[Field("実行ID", f"`{result.execution_id}`")]))

        await run_command(interaction, work, overrides=_RESUME_OVERRIDES)

    @app_commands.command(name="delete", description="あなたのコントラクトを削除し、ストレージを消去します。")
    async def delete(self, interaction: discord.Interaction[Any]) -> None:
        """Delete the invoker's unlocked contract and clear its storage; its versions stay, and registering again revives it."""

        async def work() -> Reply:
            ctx = bot_context(interaction)
            result = await delete_contract(ctx.engine, account_id=interaction.user.id, now=ctx.now(), idempotency_key=idempotency_key(interaction))
            fields = [
                Field("削除した版", f"`v{result.version}`", inline=True),
                Field("消去したストレージ", f"`{result.storage_keys_cleared:,}` キー、`{result.storage_bytes_cleared:,}` バイト", inline=True),
                Field("実行ID", f"`{result.execution_id}`"),
            ]
            return Reply(embed=success_embed(description="コントラクトを削除しました。", fields=fields))

        await run_command(interaction, work)


async def _gas_symbol(ctx: BotContext, currency_id: int) -> str | None:
    """The symbol of the gas currency, deleted ones included, or None when it has none."""
    gas_currency = await get_currency(ctx.engine, currency_id, include_deleted=True, now=ctx.now())
    return None if gas_currency is None else gas_currency.symbol


async def _call_embed(ctx: BotContext, owner: discord.abc.User, function: PublicFunction, result: ContractCallResult, attach: Attachment | None, symbol: str | None) -> discord.Embed:
    fields = [
        Field("関数", f"`{function.signature}`"),
        _value_field(result.value),
        Field("使用ガス", f"`{result.gas_used:,} / {result.gas_limit:,}`", inline=True),
        Field("ガス代", amount_text(result.gas_cost, await _gas_symbol(ctx, result.gas_currency_id)), inline=True),
    ]
    if attach is not None:
        fields.append(Field("支払い", amount_text(attach.amount, symbol), inline=True))
    fields.append(Field("実行ID", f"`{result.execution_id}`"))
    return success_embed("コントラクト実行完了", f"{owner.mention} のコントラクトを実行しました。", fields=fields)


def setup(bot: RapidWireBot) -> None:
    """Add the /contract group to bot.tree."""
    bot.tree.add_command(ContractGroup())
