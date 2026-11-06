"""The /swap confirmation: the quote embed with the allocation breakdown, and the view whose 確定 button re-quotes and swaps within the tolerance while キャンセル and the timeout end it."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any, Final

import discord

from rapidwire.bot.context import BotContext
from rapidwire.bot.dex_format import fee_tier_label
from rapidwire.bot.embeds import Field, amount_text, error_embed, info_embed, success_embed
from rapidwire.bot.errors import present_error
from rapidwire.bot.interaction import Reply, idempotency_key, interaction_log_extra, run_component
from rapidwire.core.errors import NoRoute, SlippageExceeded
from rapidwire.core.swap import SwapQuote, SwapRoute, quote_swap, swap

__all__ = [
    "CANCELED_MESSAGE",
    "MAX_ROUTE_FIELDS",
    "NOT_INVOKER_MESSAGE",
    "PRICE_MOVED_MESSAGE",
    "SWAP_TIMEOUT_SECONDS",
    "TIMED_OUT_MESSAGE",
    "TOLERANCE_DENOMINATOR",
    "TOLERANCE_NUMERATOR",
    "ZERO_OUTPUT_MESSAGE",
    "SwapConfirmView",
    "minimum_out",
    "quote_embed",
    "route_path",
    "swap_overrides",
]

log = logging.getLogger("rapidwire.bot")

# How long the confirmation buttons stay usable.
SWAP_TIMEOUT_SECONDS: Final = 30
# The confirmation proceeds while the re-quoted output is at least TOLERANCE_NUMERATOR / TOLERANCE_DENOMINATOR of the shown estimate, which is also the swap's min_out.
TOLERANCE_NUMERATOR: Final = 99
TOLERANCE_DENOMINATOR: Final = 100
# Routes beyond this many are summarised in one field, keeping the embed within Discord's 25 fields and 6000 characters.
MAX_ROUTE_FIELDS: Final = 10

PRICE_MOVED_MESSAGE: Final = "価格が変動したため、スワップを中断しました。再度お試しください。"
NOT_INVOKER_MESSAGE: Final = "この操作はコマンドを実行した本人しか行えません。"
CANCELED_MESSAGE: Final = "スワップはキャンセルされました。"
TIMED_OUT_MESSAGE: Final = "スワップがタイムアウトしました。"
ZERO_OUTPUT_MESSAGE: Final = "スワップで得られる通貨量が0以下です。"


def swap_overrides(error: BaseException) -> Mapping[str, str]:
    """The message overrides of a swap failure: SlippageExceeded is the price-moved message and a NoRoute whose output rounds down to zero is ZERO_OUTPUT_MESSAGE."""
    overrides = {SlippageExceeded.code: PRICE_MOVED_MESSAGE}
    if isinstance(error, NoRoute) and error.zero_output:
        overrides[NoRoute.code] = ZERO_OUTPUT_MESSAGE
    return overrides


def minimum_out(estimate: int) -> int:
    """The least output the confirmation accepts for a shown estimate: estimate * 99 // 100."""
    return estimate * TOLERANCE_NUMERATOR // TOLERANCE_DENOMINATOR


def _symbol(symbols: Mapping[int, str], currency_id: int) -> str:
    return symbols.get(currency_id, str(currency_id))


def route_path(route: SwapRoute, symbols: Mapping[int, str]) -> str:
    """The currencies of a route joined by arrows that carry each pool's fee tier ("RAPID -(1%)-> USD -(0.5%)-> GOLD")."""
    parts = [_symbol(symbols, route.hops[0].currency_in)]
    parts.extend(f"-({fee_tier_label(hop.fee_bps)})-> {_symbol(symbols, hop.currency_out)}" for hop in route.hops)
    return " ".join(parts)


def quote_embed(quote: SwapQuote) -> discord.Embed:
    """The blue スワップ確認 embed: input, estimated output, one field per route (input, output and path) up to MAX_ROUTE_FIELDS with the rest summed in one field, and the validity footer."""
    fields = [Field("スワップ元", amount_text(quote.amount_in, quote.from_symbol)), Field("スワップ先 (推定)", amount_text(quote.amount_out, quote.to_symbol))]
    routes = quote.routes
    for number, route in enumerate(routes[:MAX_ROUTE_FIELDS], start=1):
        value = f"入力: {amount_text(route.amount_in, quote.from_symbol)}\n受取: {amount_text(route.amount_out, quote.to_symbol)}\n経路: `{route_path(route, quote.symbols)}`"
        fields.append(Field("ルート" if len(routes) == 1 else f"ルート {number}", value))
    rest = routes[MAX_ROUTE_FIELDS:]
    if rest:
        total_in, total_out = sum(route.amount_in for route in rest), sum(route.amount_out for route in rest)
        fields.append(Field(f"その他の {len(rest)} ルート", f"入力: {amount_text(total_in, quote.from_symbol)}\n受取: {amount_text(total_out, quote.to_symbol)}"))
    lines = []
    if len(routes) > 1:
        lines.append(f"入力額を {len(routes)} 個のルートに分けてスワップします。")
    if quote.truncated:
        lines.append("経路の探索が上限に達したため、より良いルートがある可能性があります。")
    embed = info_embed("スワップ確認", "\n".join(lines) or None, fields=fields)
    embed.set_footer(text=f"このレートは{SWAP_TIMEOUT_SECONDS}秒間有効です。")
    return embed


class SwapConfirmView(discord.ui.View):
    """The buttons of one swap confirmation, usable only by the invoker for SWAP_TIMEOUT_SECONDS.

    確定 re-quotes and aborts when the new estimate is below minimum_out(estimate), otherwise swaps with that min_out under the idempotency key of the /swap interaction, so a repeated confirmation replays the same swap.
    Every outcome (completion, abort, failure, cancel, timeout) disables both buttons and stops the view.
    """

    def __init__(self, ctx: BotContext, origin: discord.Interaction[Any], quote: SwapQuote, *, timeout: float = SWAP_TIMEOUT_SECONDS) -> None:
        super().__init__(timeout=timeout)
        self.ctx = ctx
        self.origin = origin
        self.user_id = origin.user.id
        self.from_currency = quote.from_currency
        self.to_currency = quote.to_currency
        self.from_symbol = quote.from_symbol
        self.to_symbol = quote.to_symbol
        self.amount_in = quote.amount_in
        self.estimate = quote.amount_out
        self.min_out = minimum_out(quote.amount_out)
        self.idempotency_key = idempotency_key(origin)
        self.finished = False
        # Set once the re-quote passed, after which every confirmation goes straight to the (replayed) swap.
        self.confirmed = False
        self._lock = asyncio.Lock()

    async def interaction_check(self, interaction: discord.Interaction[Any], /) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(NOT_INVOKER_MESSAGE, ephemeral=True)
            return False
        return True

    def _finish(self) -> None:
        self.finished = True
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        self.stop()

    async def _confirm(self, interaction: discord.Interaction[Any]) -> Reply | None:
        async with self._lock:
            if self.finished and not self.confirmed:
                return None
            try:
                if not self.confirmed:
                    quote = await quote_swap(self.ctx.engine, from_currency=self.from_currency, to_currency=self.to_currency, amount_in=self.amount_in, config=self.ctx.swap_config)
                    if quote.amount_out < self.min_out:
                        self._finish()
                        return Reply(embed=error_embed(PRICE_MOVED_MESSAGE), view=self)
                    self.confirmed = True
                result = await swap(
                    self.ctx.engine,
                    user_id=self.user_id,
                    from_currency=self.from_currency,
                    to_currency=self.to_currency,
                    amount_in=self.amount_in,
                    min_out=self.min_out,
                    config=self.ctx.swap_config,
                    now=self.ctx.now(),
                    idempotency_key=self.idempotency_key,
                )
            except Exception as error:
                self._finish()
                return Reply(embed=present_error(error, overrides=swap_overrides(error), log_extra=interaction_log_extra(interaction)), view=self)
            self._finish()
            description = f"{amount_text(result.amount_in, self.from_symbol)} を {amount_text(result.amount_out, self.to_symbol)} にスワップしました。"
            return Reply(embed=success_embed("スワップ完了", description, fields=[Field("実行ID", f"`{result.execution_id}`")]), view=self)

    async def _cancel(self) -> Reply | None:
        async with self._lock:
            if self.finished:
                return None
            self._finish()
            return Reply(embed=error_embed(CANCELED_MESSAGE), view=self)

    @discord.ui.button(label="確定", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction[Any], button: discord.ui.Button[Any]) -> None:
        await run_component(interaction, lambda: self._confirm(interaction))

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.grey)
    async def cancel(self, interaction: discord.Interaction[Any], button: discord.ui.Button[Any]) -> None:
        await run_component(interaction, self._cancel)

    async def on_timeout(self) -> None:
        """Replace the confirmation with the timeout message and disabled buttons unless an outcome came first; a failed edit is logged."""
        async with self._lock:
            if self.finished:
                return
            self._finish()
        try:
            await self.origin.edit_original_response(embed=error_embed(TIMED_OUT_MESSAGE), view=self)
        except discord.HTTPException:
            log.warning("could not show the swap timeout", exc_info=True, extra={"interaction_id": self.origin.id})
