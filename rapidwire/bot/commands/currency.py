"""The /currency group: create, info, mint, burn, renounce, rate changes, deletion and delegation."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Final

import discord
from discord import app_commands

from rapidwire.bot.autocomplete import symbol_autocomplete
from rapidwire.bot.client import RapidWireBot, bot_context
from rapidwire.bot.context import BotContext
from rapidwire.bot.embeds import Field, account_mention, amount_text, info_embed, success_embed, timestamp_full, timestamp_relative
from rapidwire.bot.errors import UserInputError, present_error
from rapidwire.bot.interaction import Reply, idempotency_key, require_guild_id, run_command
from rapidwire.bot.symbols import normalize_symbol, resolve_currency
from rapidwire.core import currency as currency_service
from rapidwire.core.amounts import parse_amount
from rapidwire.core.currency import CurrencyInfo, get_currency, validate_currency_name, validate_currency_symbol
from rapidwire.core.errors import (
    AlreadyExists,
    CurrencyNotFound,
    CurrencyRenounced,
    InsufficientBalance,
    InvalidArgument,
    InvalidState,
    NoPendingRequest,
    PermissionDenied,
    RequestExpired,
    TimelockActive,
)
from rapidwire.db.tables import RATE_PPM_MAX

__all__ = [
    "MANAGER_OVERRIDES",
    "RATE_DECIMALS",
    "RATE_PPM_PER_PERCENT",
    "build_group",
    "format_rate",
    "info_fields",
    "parse_rate_percent",
    "setup",
]

# Rates are typed as a percent per hour with at most RATE_DECIMALS decimals; one percent is RATE_PPM_PER_PERCENT ppm.
RATE_DECIMALS: Final = 4
RATE_PPM_PER_PERCENT: Final = 10**RATE_DECIMALS
_RATE_RE: Final = re.compile(rf"^(0|[1-9][0-9]*)(\.[0-9]{{1,{RATE_DECIMALS}}})?$")
_RATE_TEXT_MAX: Final = 16
_RATE_MESSAGE: Final = f"利率は 0 から 100 までの数値 (%/h) で、小数点以下 {RATE_DECIMALS} 桁までで指定してください (例: 0.5)。符号、指数表記、空白、先頭の余分な 0 は使えません。"

_NO_CURRENCY: Final = "このサーバーには通貨が存在しません。"
_NAME_RULES: Final = "通貨の名前は24文字以内で、英字で始まり英数字で終わる必要があります。記号は `_` だけを使え、連続させず5個まで使えます。"
_SYMBOL_RULES: Final = "シンボルは8文字以内の英大文字と `-` で指定してください。`-` は連続させず2個まで使え、先頭と末尾には置けません。"

# Messages of the management commands (mint, renounce, rate changes, deletion), whose permission the core checks.
MANAGER_OVERRIDES: Final[Mapping[str, str]] = {
    PermissionDenied.code: "この操作を行えるのは、サーバーの所有者と、管理の委任が有効な場合の管理者だけです。",
    CurrencyNotFound.code: _NO_CURRENCY,
}
_OWNER_OVERRIDES: Final[Mapping[str, str]] = {
    PermissionDenied.code: "管理の委任を切り替えられるのはサーバーの所有者だけです。",
    CurrencyNotFound.code: _NO_CURRENCY,
}
_CREATE_OVERRIDES: Final[Mapping[str, str]] = {
    PermissionDenied.code: "通貨を発行できるのはサーバーの所有者だけです。",
    AlreadyExists.code: "このサーバーには既に通貨が存在するか、そのシンボルは使用済みです。",
}
_MINT_OVERRIDES: Final[Mapping[str, str]] = {**MANAGER_OVERRIDES, CurrencyRenounced.code: "この通貨のMint機能は放棄されています。"}
_BURN_OVERRIDES: Final[Mapping[str, str]] = {CurrencyNotFound.code: _NO_CURRENCY, InsufficientBalance.code: "焼却するための残高が不足しています。"}
_RENOUNCE_OVERRIDES: Final[Mapping[str, str]] = {**MANAGER_OVERRIDES, CurrencyRenounced.code: "この通貨の機能は既に放棄されています。"}
_RATE_REQUEST_OVERRIDES: Final[Mapping[str, str]] = {
    **MANAGER_OVERRIDES,
    CurrencyRenounced.code: "この通貨の利率変更は放棄されています。",
    InvalidState.code: "既に予約されている利率変更があります。適用してから再度予約してください。",
}
_RATE_APPLY_OVERRIDES: Final[Mapping[str, str]] = {
    **MANAGER_OVERRIDES,
    CurrencyRenounced.code: "この通貨の利率変更は放棄されています。",
    NoPendingRequest.code: "予約されている利率変更はありません。",
}
_DELETE_OVERRIDES: Final[Mapping[str, str]] = {
    **MANAGER_OVERRIDES,
    RequestExpired.code: "削除要請から10日以上が経過したため、この削除要請は無効になりました。\n再度削除を要請してください。",
    NoPendingRequest.code: "削除要請がありません。もう一度このコマンドを実行して削除を要請してください。",
    InvalidState.code: "既に削除要請があります。",
}


def parse_rate_percent(text: str) -> int:
    """Convert an hourly rate typed in percent ("0.5") exactly to ppm (5000), raising UserInputError for a malformed value, more than RATE_DECIMALS decimals, or a rate above 100%/h."""
    if len(text) > _RATE_TEXT_MAX or _RATE_RE.fullmatch(text) is None:
        raise UserInputError(_RATE_MESSAGE)
    whole, _, fraction = text.partition(".")
    ppm = int(whole) * RATE_PPM_PER_PERCENT + int(fraction.ljust(RATE_DECIMALS, "0"))
    if ppm > RATE_PPM_MAX:
        raise UserInputError(_RATE_MESSAGE)
    return ppm


def format_rate(ppm: int, *, unit: str = "%/h") -> str:
    """An hourly rate in ppm as a percent with RATE_DECIMALS decimals followed by unit ("0.5000%/h"); the rate change messages use the unit "%/hour"."""
    return f"{ppm // RATE_PPM_PER_PERCENT}.{ppm % RATE_PPM_PER_PERCENT:0{RATE_DECIMALS}d}{unit}"


def _moment(moment: Any) -> str:
    return f"{timestamp_full(moment)} ({timestamp_relative(moment)})"


async def _guild_currency(ctx: BotContext, interaction: discord.Interaction[Any]) -> CurrencyInfo:
    """The live currency of the guild the command runs in; UserInputError in direct messages or when the guild has none."""
    guild_id = require_guild_id(interaction)
    info = await get_currency(ctx.engine, guild_id, now=ctx.now())
    if info is None:
        raise UserInputError(_NO_CURRENCY)
    return info


def _validated_name(name: str) -> str:
    try:
        return validate_currency_name(name)
    except InvalidArgument:
        raise UserInputError(_NAME_RULES) from None


def _validated_symbol(symbol: str) -> str:
    try:
        return validate_currency_symbol(normalize_symbol(symbol))
    except InvalidArgument:
        raise UserInputError(_SYMBOL_RULES) from None


def info_fields(info: CurrencyInfo) -> list[Field]:
    """The fields of /currency info: ID, creator, supply, rate, pending rate change, renounce state, delegation, interest halt and delete request."""
    fields = [
        Field("発行サーバーID (通貨ID)", f"`{info.currency_id}`"),
        Field("作成者", account_mention(info.created_by)),
        Field("総供給量", amount_text(info.supply)),
        Field("ステーキング利率", f"`{format_rate(info.hourly_rate_ppm)}`"),
    ]
    if info.pending_rate_ppm is not None and info.rate_change_requested_at is not None and info.rate_change_apply_after is not None:
        fields.append(Field("次期ステーキング利率", f"`{format_rate(info.pending_rate_ppm)}`"))
        fields.append(Field("利率変更要求日時", timestamp_full(info.rate_change_requested_at), inline=True))
        fields.append(Field("利率変更の適用可能日時", timestamp_full(info.rate_change_apply_after), inline=True))
    fields.append(Field("Mint/利率変更 放棄状態", "はい" if info.minting_renounced else "いいえ", inline=True))
    fields.append(Field("管理の委任", "有効" if info.delegation_enabled else "無効", inline=True))
    if info.interest_halted:
        fields.append(Field("利息の発生", "停止中 (供給量が上限に達しました)", inline=True))
    if info.delete_requested_at is not None and info.delete_finalize_from is not None and info.delete_finalize_until is not None:
        fields.append(Field("削除要求日時", timestamp_full(info.delete_requested_at), inline=True))
        fields.append(Field("削除の確定可能期間", f"{timestamp_full(info.delete_finalize_from)} から {timestamp_full(info.delete_finalize_until)} まで"))
    return fields


async def _delete(ctx: BotContext, interaction: discord.Interaction[Any]) -> Reply:
    """Request deletion when the guild currency has no request, otherwise finalize it, as the earlier bot's toggle did.

    A lapsed request goes to finalization too, which fails with RequestExpired and clears the request, so the run after that requests again.
    """
    info = await _guild_currency(ctx, interaction)
    user_id = interaction.user.id
    if info.delete_requested_at is None and not info.delete_request_lapsed:
        requested = await currency_service.request_delete(ctx.engine, ctx.guild_info, currency_id=info.currency_id, user_id=user_id, now=ctx.now(), idempotency_key=idempotency_key(interaction))
        description = f"通貨 **{info.symbol}** の削除要請を受け付けました。\n**7日後から10日後まで**の間に再度このコマンドを実行すると、削除が確定します。"
        window = f"{timestamp_full(requested.finalize_from)} から {timestamp_full(requested.finalize_until)} まで"
        return Reply(embed=success_embed("削除要請完了", description, fields=[Field("確定可能期間", window), Field("実行ID", f"`{requested.execution_id}`")]))
    try:
        deleted = await currency_service.finalize_delete(ctx.engine, ctx.guild_info, currency_id=info.currency_id, user_id=user_id, now=ctx.now(), idempotency_key=idempotency_key(interaction))
    except TimelockActive as error:
        finalize_from = info.delete_finalize_from
        text = "削除の確定には、削除要請から7日間が経過している必要があります。"
        if finalize_from is not None:
            text += f"\n確定可能になる日時: {_moment(finalize_from)}"
        return Reply(embed=present_error(error, overrides={**_DELETE_OVERRIDES, TimelockActive.code: text}))
    description = f"通貨 **{info.symbol}** を完全に削除しました。\n{deleted.balances_burned}件の残高と{deleted.stakes_burned}件のステークを焼却しました。"
    fields = [
        Field("焼却した総額", amount_text(deleted.burned_amount, info.symbol)),
        Field("解散したプール", f"`{deleted.pools_dissolved}`", inline=True),
        Field("取り消した請求", f"`{deleted.claims_canceled}`", inline=True),
        Field("削除した承認", f"`{deleted.allowances_deleted}`", inline=True),
        Field("実行ID", f"`{deleted.execution_id}`"),
    ]
    return Reply(embed=success_embed("通貨削除完了", description, fields=fields))


def build_group() -> app_commands.Group:
    """A new /currency group with its delegation subgroup."""
    group = app_commands.Group(name="currency", description="通貨に関連するコマンド")
    delegation = app_commands.Group(name="delegation", description="管理者権限を持つユーザーへの通貨の管理の委任を切り替えます。", parent=group)

    @group.command(name="create", description="このサーバーに新しい通貨を発行します。")
    @app_commands.describe(name="通貨の名前", symbol="通貨のシンボル", supply="初期供給量", hourly_interest_rate="ステーキングの利率 (%/hour、小数点以下4桁まで、100まで)")
    async def create(interaction: discord.Interaction[Any], name: str, symbol: str, supply: str, hourly_interest_rate: str | None = None) -> None:
        async def work() -> Reply:
            ctx = bot_context(interaction)
            guild_id = require_guild_id(interaction)
            checked_name = _validated_name(name)
            checked_symbol = _validated_symbol(symbol)
            initial_supply = parse_amount(supply, allow_zero=True)
            rate_ppm = 0 if hourly_interest_rate is None else parse_rate_percent(hourly_interest_rate)
            created = await currency_service.create_currency(
                ctx.engine,
                ctx.guild_info,
                guild_id=guild_id,
                user_id=interaction.user.id,
                name=checked_name,
                symbol=checked_symbol,
                initial_supply=initial_supply,
                hourly_rate_ppm=rate_ppm,
                now=ctx.now(),
                idempotency_key=idempotency_key(interaction),
            )
            fields = [Field("総供給量", amount_text(created.initial_supply)), Field("ステーキングの利率", f"`{format_rate(created.hourly_rate_ppm)}`")]
            if created.transfer_id is not None:
                fields.append(Field("初期供給の転送ID", f"`{created.transfer_id}`"))
            fields.append(Field("実行ID", f"`{created.execution_id}`"))
            description = f"新しい通貨 **{created.name} ({created.symbol})** が発行されました。"
            return Reply(embed=success_embed("通貨発行成功", description, fields=fields))

        await run_command(interaction, work, overrides=_CREATE_OVERRIDES)

    @group.command(name="info", description="通貨の詳細情報を表示します。")
    @app_commands.describe(symbol="情報を表示する通貨のシンボル (省略するとこのサーバーの通貨)")
    @app_commands.autocomplete(symbol=symbol_autocomplete)
    async def info(interaction: discord.Interaction[Any], symbol: str | None = None) -> None:
        async def work() -> Reply:
            found = await resolve_currency(bot_context(interaction), interaction, symbol)
            return Reply(embed=info_embed(f"通貨情報: {found.name} ({found.symbol})", fields=info_fields(found)))

        await run_command(interaction, work)

    @group.command(name="mint", description="[管理者] 通貨を追加発行します。")
    @app_commands.describe(amount="追加発行する量")
    async def mint(interaction: discord.Interaction[Any], amount: str) -> None:
        async def work() -> Reply:
            ctx = bot_context(interaction)
            found = await _guild_currency(ctx, interaction)
            units = parse_amount(amount, allow_zero=False)
            result = await currency_service.mint(
                ctx.engine, ctx.guild_info, currency_id=found.currency_id, user_id=interaction.user.id, amount=units, now=ctx.now(), idempotency_key=idempotency_key(interaction)
            )
            return Reply(embed=success_embed("Mint成功", f"{amount_text(result.amount, found.symbol)} を追加発行しました。", fields=[Field("実行ID", f"`{result.execution_id}`")]))

        await run_command(interaction, work, overrides=_MINT_OVERRIDES)

    @group.command(name="burn", description="保有する通貨を焼却します。")
    @app_commands.describe(amount="焼却する量")
    async def burn(interaction: discord.Interaction[Any], amount: str) -> None:
        async def work() -> Reply:
            ctx = bot_context(interaction)
            found = await _guild_currency(ctx, interaction)
            units = parse_amount(amount, allow_zero=False)
            result = await currency_service.burn(ctx.engine, account_id=interaction.user.id, currency_id=found.currency_id, amount=units, now=ctx.now(), idempotency_key=idempotency_key(interaction))
            return Reply(embed=success_embed("Burn成功", f"{amount_text(result.amount, found.symbol)} を焼却しました。", fields=[Field("実行ID", f"`{result.execution_id}`")]))

        await run_command(interaction, work, overrides=_BURN_OVERRIDES)

    @group.command(name="renounce", description="[管理者] Mintと利率変更機能を永久に放棄します。")
    async def renounce(interaction: discord.Interaction[Any]) -> None:
        async def work() -> Reply:
            ctx = bot_context(interaction)
            found = await _guild_currency(ctx, interaction)
            result = await currency_service.renounce(
                ctx.engine, ctx.guild_info, currency_id=found.currency_id, user_id=interaction.user.id, now=ctx.now(), idempotency_key=idempotency_key(interaction)
            )
            description = f"**{found.symbol}** のMint機能と利率変更機能を永久に放棄しました。この操作は取り消せません。"
            return Reply(embed=success_embed("機能放棄", description, fields=[Field("実行ID", f"`{result.execution_id}`")]))

        await run_command(interaction, work, overrides=_RENOUNCE_OVERRIDES)

    @group.command(name="delete", description="[管理者] このサーバーの通貨の削除を要請し、7日後から10日後までの再実行で確定します。")
    async def delete(interaction: discord.Interaction[Any]) -> None:
        async def work() -> Reply:
            return await _delete(bot_context(interaction), interaction)

        await run_command(interaction, work, overrides=_DELETE_OVERRIDES)

    @group.command(name="request-interest-change", description="[管理者] ステーキングの時利変更を予約します。")
    @app_commands.describe(rate="新しい利率 (%/hour、小数点以下4桁まで、100まで)")
    async def request_interest_change(interaction: discord.Interaction[Any], rate: str) -> None:
        async def work() -> Reply:
            ctx = bot_context(interaction)
            found = await _guild_currency(ctx, interaction)
            rate_ppm = parse_rate_percent(rate)
            result = await currency_service.request_rate_change(
                ctx.engine, ctx.guild_info, currency_id=found.currency_id, user_id=interaction.user.id, new_rate_ppm=rate_ppm, now=ctx.now(), idempotency_key=idempotency_key(interaction)
            )
            rate_text = format_rate(result.new_rate_ppm, unit="%/hour")
            description = f"ステーキングの利率を`{rate_text}`に変更するリクエストを受け付けました。\nこの変更は {_moment(result.apply_after)} 以降に適用可能になります。"
            return Reply(embed=success_embed("利率変更予約完了", description, fields=[Field("実行ID", f"`{result.execution_id}`")]))

        await run_command(interaction, work, overrides=_RATE_REQUEST_OVERRIDES)

    @group.command(name="apply-interest-change", description="[管理者] 予約されている利率変更を適用します。")
    async def apply_interest_change(interaction: discord.Interaction[Any]) -> None:
        async def work() -> Reply:
            ctx = bot_context(interaction)
            found = await _guild_currency(ctx, interaction)
            try:
                result = await currency_service.apply_rate_change(
                    ctx.engine, ctx.guild_info, currency_id=found.currency_id, user_id=interaction.user.id, now=ctx.now(), idempotency_key=idempotency_key(interaction)
                )
            except TimelockActive as error:
                text = "利率変更の予約から7日間が経過していないため、まだ適用できません。"
                if found.rate_change_apply_after is not None:
                    text += f"\n適用可能になる日時: {_moment(found.rate_change_apply_after)}"
                return Reply(embed=present_error(error, overrides={**_RATE_APPLY_OVERRIDES, TimelockActive.code: text}))
            description = f"ステーキングの利率が `{format_rate(result.new_rate_ppm, unit='%/hour')}` に正常に更新されました。"
            fields = [Field("旧利率で計上したステーク", f"`{result.stakes_accrued}`"), Field("実行ID", f"`{result.execution_id}`")]
            return Reply(embed=success_embed("利率変更適用完了", description, fields=fields))

        await run_command(interaction, work, overrides=_RATE_APPLY_OVERRIDES)

    @delegation.command(name="enable", description="[所有者] 管理者権限を持つユーザーに通貨の管理を委任します。")
    async def delegation_enable(interaction: discord.Interaction[Any]) -> None:
        await run_command(interaction, lambda: _set_delegation(interaction, enabled=True), overrides=_OWNER_OVERRIDES)

    @delegation.command(name="disable", description="[所有者] 通貨の管理の委任をやめ、所有者だけが管理できるようにします。")
    async def delegation_disable(interaction: discord.Interaction[Any]) -> None:
        await run_command(interaction, lambda: _set_delegation(interaction, enabled=False), overrides=_OWNER_OVERRIDES)

    return group


async def _set_delegation(interaction: discord.Interaction[Any], *, enabled: bool) -> Reply:
    ctx = bot_context(interaction)
    found = await _guild_currency(ctx, interaction)
    service = currency_service.enable_delegation if enabled else currency_service.disable_delegation
    result = await service(ctx.engine, ctx.guild_info, currency_id=found.currency_id, user_id=interaction.user.id, now=ctx.now(), idempotency_key=idempotency_key(interaction))
    if enabled:
        description = f"このサーバーの管理者権限を持つユーザーが、通貨 **{found.symbol}** を管理できるようになりました。" if result.changed else "管理の委任は既に有効です。"
    else:
        description = f"通貨 **{found.symbol}** を管理できるのは、サーバーの所有者だけになりました。" if result.changed else "管理の委任は既に無効です。"
    return Reply(embed=success_embed("管理の委任を有効化" if enabled else "管理の委任を無効化", description, fields=[Field("実行ID", f"`{result.execution_id}`")]))


def setup(bot: RapidWireBot) -> None:
    """Add the /currency group to bot.tree."""
    bot.tree.add_command(build_group())
