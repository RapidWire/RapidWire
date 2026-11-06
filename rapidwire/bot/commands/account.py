"""Account commands: /balance, /transfer, /transfer_from and /history, plus load_page(), the page loader the paged list commands share."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from datetime import date, datetime, time, timedelta, timezone
from typing import Final

import discord
from discord import app_commands

from rapidwire.bot.autocomplete import MAX_CHOICES, symbol_autocomplete
from rapidwire.bot.client import RapidWireBot, bot_context
from rapidwire.bot.embeds import ITEMS_PER_PAGE, Field, account_mention, amount_text, info_embed, plain_text, success_embed, timestamp_full, timestamp_relative
from rapidwire.bot.errors import UserInputError
from rapidwire.bot.interaction import Reply, idempotency_key, run_command
from rapidwire.bot.paging import check_page, page_title
from rapidwire.bot.symbols import resolve_currency
from rapidwire.core import errors as core_errors
from rapidwire.core import queries, transfers
from rapidwire.core.amounts import NumberFormatError, parse_amount
from rapidwire.core.paging import HISTORY_PER_PAGE_DEFAULT, MAX_WINDOW, Page
from rapidwire.core.queries import TransferEntry
from rapidwire.core.types import ExecutionStatus, ExecutionType, Origin, TransferKind
from rapidwire.db.tables import CONTRACT_FUNCTION_MAX

__all__ = [
    "EXECUTION_STATUS_LABELS",
    "EXECUTION_TYPE_LABELS",
    "HISTORY_TIMEZONE",
    "LEGACY_MARK",
    "date_bounds",
    "execution_type_autocomplete",
    "load_page",
    "setup",
]

# History dates are calendar days in Japan Standard Time; Japan has no daylight saving time, so a fixed offset is exact.
HISTORY_TIMEZONE: Final = timezone(timedelta(hours=9), "JST")

# Marks transfers of executions imported from the earlier version.
LEGACY_MARK: Final = "(旧版)"

HISTORY_PER_PAGE: Final = HISTORY_PER_PAGE_DEFAULT

EXECUTION_TYPE_LABELS: Final[Mapping[str, str]] = {
    ExecutionType.TRANSFER: "送金",
    ExecutionType.TRANSFER_FROM: "代理送金",
    ExecutionType.APPROVE: "許可額の設定",
    ExecutionType.MINT: "Mint",
    ExecutionType.BURN: "焼却",
    ExecutionType.CURRENCY_CREATE: "通貨の作成",
    ExecutionType.CURRENCY_RENOUNCE: "Mintと利率変更の放棄",
    ExecutionType.RATE_CHANGE_REQUEST: "利率変更の予約",
    ExecutionType.RATE_CHANGE_APPLY: "利率変更の適用",
    ExecutionType.CURRENCY_DELETE_REQUEST: "通貨の削除要求",
    ExecutionType.CURRENCY_DELETE_FINALIZE: "通貨の削除の確定",
    ExecutionType.DELEGATION_ENABLE: "管理の委任の有効化",
    ExecutionType.DELEGATION_DISABLE: "管理の委任の無効化",
    ExecutionType.STAKE_DEPOSIT: "ステーク預け入れ",
    ExecutionType.STAKE_WITHDRAW: "ステーク引き出し",
    ExecutionType.INTEREST: "利息計上",
    ExecutionType.CLAIM_CREATE: "請求の作成",
    ExecutionType.CLAIM_PAY: "請求の支払い",
    ExecutionType.CLAIM_CANCEL: "請求の取り消し",
    ExecutionType.NOTIFICATION_ALLOW: "請求通知の許可",
    ExecutionType.NOTIFICATION_DENY: "請求通知の拒否",
    ExecutionType.DISCORD_PERMISSION_ALLOW: "Discord操作の許可",
    ExecutionType.DISCORD_PERMISSION_DENY: "Discord操作の禁止",
    ExecutionType.TOKEN_ISSUE: "APIトークンの発行",
    ExecutionType.TOKEN_REVOKE: "APIトークンの失効",
    ExecutionType.POOL_CREATE: "プール作成",
    ExecutionType.LIQUIDITY_ADD: "流動性追加",
    ExecutionType.LIQUIDITY_REMOVE: "流動性削除",
    ExecutionType.SWAP: "スワップ",
    ExecutionType.CONTRACT_CALL: "コントラクト実行",
    ExecutionType.CONTRACT_UPDATE: "コントラクト更新",
    ExecutionType.CONTRACT_LOCK: "コントラクトのロック",
    ExecutionType.CONTRACT_PAUSE: "コントラクトの一時停止",
    ExecutionType.CONTRACT_RESUME: "コントラクトの再開",
    ExecutionType.CONTRACT_DELETE: "コントラクトの削除",
    ExecutionType.BACKUP_LIST: "バックアップの一覧取得",
    ExecutionType.BACKUP_DOWNLOAD: "バックアップの取得",
}

EXECUTION_STATUS_LABELS: Final[Mapping[str, str]] = {
    ExecutionStatus.SUCCESS: "成功",
    ExecutionStatus.FAILED: "失敗",
    ExecutionStatus.REVERTED: "取り消し",
}

_KIND_LABELS: Final[Mapping[str, str]] = {TransferKind.INTEREST: "利息", TransferKind.GAS: "ガス代"}

_DATE_RE: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


async def load_page[T](page: int, per_page: int, fetch: Callable[[int], Awaitable[Page[T]]]) -> tuple[Page[T], int]:
    """Fetch one page of a list and its page count, raising the earlier bot's out-of-range UserInputError for a page below 1 or past the last page, and UserInputError for a page beyond the first MAX_WINDOW items; with no items the page is returned empty for the caller's empty message."""
    if page >= 1 and page * per_page <= MAX_WINDOW:
        result = await fetch(page)
        return result, check_page(page, result.total, per_page=per_page)
    first = await fetch(1)
    pages = check_page(page, first.total, per_page=per_page)
    if first.total == 0:
        return first, pages
    raise UserInputError(f"先頭の {MAX_WINDOW:,} 件より後のページは表示できません。条件を絞り込んでください。")


async def _balance(interaction: discord.Interaction[RapidWireBot], user: discord.User | None, page: int) -> Reply:
    ctx = bot_context(interaction)
    target = user or interaction.user
    result, pages = await load_page(page, ITEMS_PER_PAGE, lambda number: queries.list_balances(ctx.engine, target.id, page=number, per_page=ITEMS_PER_PAGE))
    if not result.items:
        return Reply(embed=success_embed("残高", f"{plain_text(target.display_name)}は資産を保有していません。"))
    fields = [Field(f"{item.name} ({item.symbol})", amount_text(item.amount)) for item in result.items]
    return Reply(embed=success_embed(page_title(f"{target.display_name}の保有資産", page, pages), fields=fields))


@app_commands.command(name="balance", description="あなたの保有資産を表示します。")
@app_commands.describe(user="残高を表示するユーザー", page="ページ番号")
async def balance(interaction: discord.Interaction[RapidWireBot], user: discord.User | None = None, page: int = 1) -> None:
    await run_command(interaction, lambda: _balance(interaction, user, page))


def _transfer_amount(amount: str) -> int:
    """Parse a /transfer or /transfer_from amount, with the earlier bot's message for zero and negative amounts."""
    try:
        return parse_amount(amount, allow_zero=False)
    except NumberFormatError as error:
        if error.kind in ("amount_zero", "amount_negative"):
            raise UserInputError("送金額は0より大きい必要があります。") from None
        raise


async def _transfer(interaction: discord.Interaction[RapidWireBot], user: discord.User, amount: str, symbol: str | None) -> Reply:
    ctx = bot_context(interaction)
    units = _transfer_amount(amount)
    info = await resolve_currency(ctx, interaction, symbol)
    result = await transfers.transfer(
        ctx.engine, sender_id=interaction.user.id, recipient_id=user.id, currency_id=info.currency_id, amount=units, now=ctx.now(), idempotency_key=idempotency_key(interaction)
    )
    description = f"{user.mention} へ {amount_text(result.amount, info.symbol)} の送金が完了しました。"
    return Reply(embed=success_embed("送金完了", description, fields=[Field("転送ID", f"`{result.transfer_id}`")]))


@app_commands.command(name="transfer", description="指定したユーザーに通貨を送金します。")
@app_commands.describe(user="送金先のユーザー", amount="送金する量", symbol="送金する通貨のシンボル")
@app_commands.autocomplete(symbol=symbol_autocomplete)
async def transfer(interaction: discord.Interaction[RapidWireBot], user: discord.User, amount: str, symbol: str | None = None) -> None:
    overrides = {core_errors.InvalidArgument.code: "自分自身には送金できません。"}
    await run_command(interaction, lambda: _transfer(interaction, user, amount, symbol), overrides=overrides)


async def _transfer_from(interaction: discord.Interaction[RapidWireBot], source: discord.User, destination: discord.User, amount: str, symbol: str | None) -> Reply:
    ctx = bot_context(interaction)
    units = _transfer_amount(amount)
    info = await resolve_currency(ctx, interaction, symbol)
    result = await transfers.transfer_from(
        ctx.engine,
        spender_id=interaction.user.id,
        owner_id=source.id,
        recipient_id=destination.id,
        currency_id=info.currency_id,
        amount=units,
        now=ctx.now(),
        idempotency_key=idempotency_key(interaction),
    )
    description = f"{source.mention} から {destination.mention} へ {amount_text(result.amount, info.symbol)} の代理送金が完了しました。"
    fields = [Field("転送ID", f"`{result.transfer_id}`"), Field("実行ID", f"`{result.execution_id}`")]
    return Reply(embed=success_embed("代理送金完了", description, fields=fields))


@app_commands.command(name="transfer_from", description="他のユーザーのウォレットから送金します (要承認)")
@app_commands.describe(source="送金元ユーザー", destination="送金先ユーザー", amount="送金する量", symbol="送金する通貨のシンボル")
@app_commands.autocomplete(symbol=symbol_autocomplete)
async def transfer_from(interaction: discord.Interaction[RapidWireBot], source: discord.User, destination: discord.User, amount: str, symbol: str | None = None) -> None:
    overrides = {core_errors.InvalidArgument.code: "送金元は実行者自身とも送金先とも異なるユーザーである必要があります。自分の資産は /transfer で送金してください。"}
    await run_command(interaction, lambda: _transfer_from(interaction, source, destination, amount, symbol), overrides=overrides)


def _given_text(text: str | None) -> str | None:
    """The text of an optional option, or None when it is missing or blank."""
    return text if text is not None and text.strip() else None


def _optional_amount(text: str | None) -> int | None:
    """An optional amount filter: None when missing or blank, otherwise parsed exactly as typed, like every amount option."""
    given = _given_text(text)
    return parse_amount(given, allow_zero=True) if given is not None else None


def _parse_date(text: str, name: str) -> date:
    if _DATE_RE.fullmatch(text) is None:
        raise UserInputError(f"{name}は YYYY-MM-DD の形式で指定してください (例: 2026-01-31)。")
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise UserInputError(f"{name}に存在しない日付が指定されました。") from None


def date_bounds(start_date: str | None, end_date: str | None) -> tuple[datetime | None, datetime | None]:
    """The inclusive start and exclusive end of a YYYY-MM-DD date range in HISTORY_TIMEZONE: start_date's midnight and the midnight after end_date; a missing or blank date leaves its bound open, surrounding whitespace is ignored, and UserInputError is raised for malformed dates and for an end before the start."""
    start = end = None
    start_text, end_text = _given_text(start_date), _given_text(end_date)
    if start_text is not None:
        start = datetime.combine(_parse_date(start_text.strip(), "開始日"), time(), HISTORY_TIMEZONE)
    if end_text is not None:
        last = _parse_date(end_text.strip(), "終了日")
        if last == date.max:
            raise UserInputError("終了日に存在しない日付が指定されました。")
        end = datetime.combine(last + timedelta(days=1), time(), HISTORY_TIMEZONE)
    if start is not None and end is not None and end <= start:
        raise UserInputError("終了日には開始日以降の日付を指定してください。")
    return start, end


def _type_label(execution_type: str) -> str:
    label = EXECUTION_TYPE_LABELS.get(execution_type)
    return f"{label} (`{execution_type}`)" if label is not None else f"`{execution_type}`"


async def execution_type_autocomplete(interaction: discord.Interaction[RapidWireBot], current: str) -> list[app_commands.Choice[str]]:
    """Execution types whose value or Japanese label contains the typed text, at most MAX_CHOICES, in the enum's order."""
    typed = current.strip().lower()
    found = [item for item in ExecutionType if typed in item.value or typed in EXECUTION_TYPE_LABELS[item].lower()]
    return [app_commands.Choice(name=f"{item.value} ({EXECUTION_TYPE_LABELS[item]})", value=item.value) for item in found[:MAX_CHOICES]]


def _transfer_detail(entry: TransferEntry) -> discord.Embed:
    title = f"転送詳細: ID {entry.transfer_id}"
    if entry.origin is Origin.LEGACY:
        title = f"{title} {LEGACY_MARK}"
    fields = [
        Field("日時", timestamp_full(entry.created_at)),
        Field("From", account_mention(entry.from_account), inline=True),
        Field("To", account_mention(entry.to_account), inline=True),
        Field("金額", amount_text(entry.amount, entry.currency_symbol)),
    ]
    if entry.kind in _KIND_LABELS:
        fields.append(Field("移動の種類", _KIND_LABELS[entry.kind]))
    fields += [
        Field("実行ID", f"`{entry.execution_id}`"),
        Field("実行の種別", _type_label(entry.execution_type), inline=True),
        Field("実行の状態", EXECUTION_STATUS_LABELS.get(entry.execution_status, entry.execution_status), inline=True),
    ]
    if entry.contract_function is not None:
        fields.append(Field("コントラクトの関数", f"`{entry.contract_function.replace('`', '')}`"))
    if entry.origin is Origin.LEGACY:
        fields.append(Field("由来", "旧版から取り込んだ実行"))
    return info_embed(title, fields=fields)


def _history_field(entry: TransferEntry, target_id: int) -> Field:
    source, destination = account_mention(entry.from_account), account_mention(entry.to_account)
    if entry.from_account == target_id:
        emoji, direction = "📤", f"to {destination}"
    elif entry.to_account == target_id:
        emoji, direction = "📥", f"from {source}"
    else:
        emoji, direction = "↔️", f"from {source} to {destination}"
    name = f"{emoji} | ID: {entry.transfer_id} | {timestamp_relative(entry.created_at)}"
    if entry.origin is Origin.LEGACY:
        name = f"{name} {LEGACY_MARK}"
    value = f"{amount_text(entry.amount, entry.currency_symbol)} {direction}"
    if entry.kind in _KIND_LABELS:
        value = f"{value} ({_KIND_LABELS[entry.kind]})"
    return Field(name, value)


async def _history(
    interaction: discord.Interaction[RapidWireBot],
    *,
    transfer_id: int | None,
    user: discord.User | None,
    source: discord.User | None,
    destination: discord.User | None,
    currency_symbol: str | None,
    start_date: str | None,
    end_date: str | None,
    min_amount: str | None,
    max_amount: str | None,
    execution_type: str | None,
    contract_function: str | None,
    page: int,
) -> Reply:
    ctx = bot_context(interaction)
    if transfer_id is not None:
        entry = await queries.get_transfer(ctx.engine, transfer_id) if transfer_id >= 1 else None
        if entry is None:
            raise UserInputError("指定された転送IDは見つかりませんでした。")
        return Reply(embed=_transfer_detail(entry))

    user_id = user.id if user is not None else None
    if user is None and source is None and destination is None:
        user_id = interaction.user.id
    target = user or source or destination or interaction.user
    currency_id = None
    if currency_symbol is not None and currency_symbol.strip():
        currency_id = (await resolve_currency(ctx, interaction, currency_symbol)).currency_id
    start, end = date_bounds(start_date, end_date)
    low, high = _optional_amount(min_amount), _optional_amount(max_amount)
    if low is not None and high is not None and high < low:
        raise UserInputError("最大金額には最小金額以上の値を指定してください。")
    wanted_type = None
    if execution_type is not None and execution_type.strip():
        wanted_type = execution_type.strip().lower()
        if wanted_type not in EXECUTION_TYPE_LABELS:
            raise UserInputError("実行の種別は候補の中から選んでください。")
    function = None
    if contract_function is not None and contract_function.strip():
        function = contract_function.strip()
        if len(function) > CONTRACT_FUNCTION_MAX:
            raise UserInputError(f"コントラクトの関数名は {CONTRACT_FUNCTION_MAX} 文字以内で指定してください。")

    def fetch(number: int) -> Awaitable[Page[TransferEntry]]:
        return queries.search_history(
            ctx.engine,
            user_id=user_id,
            source_id=source.id if source is not None else None,
            destination_id=destination.id if destination is not None else None,
            currency_id=currency_id,
            start=start,
            end=end,
            min_amount=low,
            max_amount=high,
            execution_type=wanted_type,
            contract_function=function,
            page=number,
            per_page=HISTORY_PER_PAGE,
        )

    result, pages = await load_page(page, HISTORY_PER_PAGE, fetch)
    if not result.items:
        return Reply(embed=success_embed("転送履歴", "指定された条件の転送履歴はありません。"))
    return Reply(embed=info_embed(page_title("転送履歴", page, pages), fields=[_history_field(entry, target.id) for entry in result.items]))


@app_commands.command(name="history", description="転送履歴を表示します。")
@app_commands.describe(
    transfer_id="詳細を表示する転送ID",
    user="対象ユーザー",
    source="送金元ユーザー",
    destination="送金先ユーザー",
    currency_symbol="通貨シンボル",
    start_date="開始日 (YYYY-MM-DD、日本時間)",
    end_date="終了日 (YYYY-MM-DD、日本時間、この日を含む)",
    min_amount="最小金額",
    max_amount="最大金額",
    execution_type="実行の種別",
    contract_function="コントラクトの関数名",
    page="ページ番号",
)
@app_commands.autocomplete(currency_symbol=symbol_autocomplete, execution_type=execution_type_autocomplete)
async def history(
    interaction: discord.Interaction[RapidWireBot],
    transfer_id: int | None = None,
    user: discord.User | None = None,
    source: discord.User | None = None,
    destination: discord.User | None = None,
    currency_symbol: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    min_amount: str | None = None,
    max_amount: str | None = None,
    execution_type: str | None = None,
    contract_function: str | None = None,
    page: int = 1,
) -> None:
    async def work() -> Reply:
        return await _history(
            interaction,
            transfer_id=transfer_id,
            user=user,
            source=source,
            destination=destination,
            currency_symbol=currency_symbol,
            start_date=start_date,
            end_date=end_date,
            min_amount=min_amount,
            max_amount=max_amount,
            execution_type=execution_type,
            contract_function=contract_function,
            page=page,
        )

    await run_command(interaction, work)


def setup(bot: RapidWireBot) -> None:
    """Add /balance, /transfer, /transfer_from and /history to bot.tree."""
    bot.tree.add_command(balance)
    bot.tree.add_command(transfer)
    bot.tree.add_command(transfer_from)
    bot.tree.add_command(history)
