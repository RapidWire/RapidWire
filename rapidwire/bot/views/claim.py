"""The claim notification direct message: its embed and persistent buttons (pay, deny, stop notifications from the claimant), built from the outbox claim_created payload for discord.py or as raw Discord REST JSON, and the buttons' handlers, which keep working after a restart because the claim ID is in each custom_id."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Self

import discord

from rapidwire.bot.client import bot_context
from rapidwire.bot.embeds import Field, amount_text, info_embed, plain_text, success_embed
from rapidwire.bot.errors import present_error
from rapidwire.bot.interaction import Reply, Work, component_idempotency_key, idempotency_key, interaction_log_extra, run_component
from rapidwire.core import claims, notifications
from rapidwire.core import errors as core_errors
from rapidwire.core.amounts import NumberFormatError, parse_int
from rapidwire.core.validation import BIGINT_MAX
from rapidwire.db.tables import CLAIM_DESCRIPTION_MAX, CURRENCY_SYMBOL_MAX
from rapidwire.db.types import AMOUNT_MAX

__all__ = [
    "CANCEL_OVERRIDES",
    "CLAIM_BUTTON_CLASSES",
    "CLAIM_CUSTOM_ID_PREFIX",
    "CLAIM_PAYLOAD_KEYS",
    "PAY_OVERRIDES",
    "ClaimDenyButton",
    "ClaimMuteButton",
    "ClaimNotification",
    "ClaimPayButton",
    "build_claim_notification",
    "canceled_embed",
    "claim_custom_id",
    "claim_notification_embed",
    "claim_notification_message",
    "paid_embed",
    "parse_claim_payload",
    "pay_reply",
]

CLAIM_CUSTOM_ID_PREFIX: Final = "rw:claim:"

# The exact keys of an outbox claim_created payload; integers are decimal strings and description is a string or null.
CLAIM_PAYLOAD_KEYS: Final = frozenset({"claim_id", "claimant_id", "payer_id", "currency_id", "currency_symbol", "amount", "description"})

PAY_OVERRIDES: Final[Mapping[str, str]] = {
    core_errors.PermissionDenied.code: "この請求を支払えるのは支払者だけです。",
    core_errors.InvalidState.code: "この請求は既に支払い済みか、キャンセルされています。",
}

CANCEL_OVERRIDES: Final[Mapping[str, str]] = {
    core_errors.PermissionDenied.code: "この請求をキャンセルできるのは請求者と支払者だけです。",
    core_errors.InvalidState.code: "この請求は既に支払い済みか、キャンセルされています。",
}

_MUTE_OVERRIDES: Final[Mapping[str, str]] = {core_errors.InvalidArgument.code: "自分自身からの通知は停止できません。"}

_ID_PATTERN: Final = r"(?P<claim_id>[1-9][0-9]{0,18})"


@dataclass(frozen=True)
class ClaimNotification:
    """A validated claim_created payload: the payer receives the message about the claimant's claim."""

    claim_id: int
    claimant_id: int
    payer_id: int
    currency_id: int
    currency_symbol: str
    amount: int
    description: str | None


def _int_field(payload: Mapping[str, Any], key: str, maximum: int) -> int:
    try:
        return parse_int(payload[key], 1, maximum)
    except NumberFormatError as error:
        raise ValueError(f"claim_created payload field {key} is invalid: {error.message}") from None


def parse_claim_payload(payload: Mapping[str, Any]) -> ClaimNotification:
    """Validate an outbox claim_created payload strictly: exactly CLAIM_PAYLOAD_KEYS, positive decimal string IDs and amount, a 1 to CURRENCY_SYMBOL_MAX character symbol and a description of at most CLAIM_DESCRIPTION_MAX characters or null; anything else raises ValueError."""
    if not isinstance(payload, Mapping):
        raise ValueError("claim_created payload must be a JSON object")
    keys = set(payload)
    if keys != CLAIM_PAYLOAD_KEYS:
        missing, unknown = sorted(CLAIM_PAYLOAD_KEYS - keys), sorted(str(key) for key in keys - CLAIM_PAYLOAD_KEYS)
        raise ValueError(f"claim_created payload keys do not match (missing: {missing}, unknown: {unknown})")
    symbol = payload["currency_symbol"]
    if not isinstance(symbol, str) or not 1 <= len(symbol) <= CURRENCY_SYMBOL_MAX:
        raise ValueError("claim_created payload field currency_symbol is invalid")
    description = payload["description"]
    if description is not None and (not isinstance(description, str) or not 1 <= len(description) <= CLAIM_DESCRIPTION_MAX):
        raise ValueError("claim_created payload field description is invalid")
    return ClaimNotification(
        claim_id=_int_field(payload, "claim_id", BIGINT_MAX),
        claimant_id=_int_field(payload, "claimant_id", BIGINT_MAX),
        payer_id=_int_field(payload, "payer_id", BIGINT_MAX),
        currency_id=_int_field(payload, "currency_id", BIGINT_MAX),
        currency_symbol=symbol,
        amount=_int_field(payload, "amount", AMOUNT_MAX),
        description=description,
    )


def claim_custom_id(action: str, claim_id: int) -> str:
    """The custom_id of a claim button: rw:claim:<action>:<claim id>, with action pay, deny or mute."""
    return f"{CLAIM_CUSTOM_ID_PREFIX}{action}:{claim_id}"


def claim_notification_embed(notice: ClaimNotification) -> discord.Embed:
    """The earlier bot's notification embed: 請求の通知 (blue) with 請求者, 金額, 説明 when given, and the footer 請求ID: <id>."""
    fields = [Field("請求者", f"<@{notice.claimant_id}>"), Field("金額", amount_text(notice.amount, notice.currency_symbol))]
    if notice.description is not None:
        fields.append(Field("説明", plain_text(notice.description)))
    embed = info_embed("請求の通知", fields=fields)
    embed.set_footer(text=f"請求ID: {notice.claim_id}")
    return embed


def paid_embed(claim_id: int, transfer_id: int) -> discord.Embed:
    """The 支払い完了 embed shown after a claim is paid, by button or by /claim pay."""
    return success_embed("支払い完了", f"請求ID `{claim_id}` の支払いが完了しました。", fields=[Field("転送ID", f"`{transfer_id}`")])


def canceled_embed(claim_id: int) -> discord.Embed:
    """The キャンセル完了 embed of /claim cancel."""
    return success_embed("キャンセル完了", f"請求ID `{claim_id}` をキャンセルしました。")


async def pay_reply(interaction: discord.Interaction[Any], claim_id: int, key: str) -> Reply:
    """Pay the claim as the interaction's user under the idempotency key and answer with paid_embed(); used by the 承認 button and /claim pay."""
    ctx = bot_context(interaction)
    result = await claims.pay_claim(ctx.engine, claim_id=claim_id, payer_id=interaction.user.id, now=ctx.now(), idempotency_key=key)
    return Reply(embed=paid_embed(result.claim_id, result.transfer_id))


async def _deny(interaction: discord.Interaction[Any], claim_id: int) -> Reply:
    ctx = bot_context(interaction)
    key = component_idempotency_key(interaction)
    result = await claims.cancel_claim(ctx.engine, claim_id=claim_id, user_id=interaction.user.id, now=ctx.now(), idempotency_key=key)
    return Reply(embed=success_embed("請求拒否", f"請求ID `{result.claim_id}` を拒否しました。"))


async def _keeping_claim_id(interaction: discord.Interaction[Any], claim_id: int, work: Work, overrides: Mapping[str, str]) -> Reply | None:
    """Run a button's work and turn a failure into the presenter's embed with the footer 請求ID: <id>, so the message that replaces the notification still names the claim for /claim pay and /claim cancel."""
    try:
        return await work()
    except Exception as error:
        embed = present_error(error, overrides=overrides, log_extra=interaction_log_extra(interaction))
        embed.set_footer(text=f"請求ID: {claim_id}")
        return Reply(embed=embed)


async def _mute(interaction: discord.Interaction[Any], claim_id: int) -> Reply:
    ctx = bot_context(interaction)
    claim = await claims.get_claim(ctx.engine, claim_id)
    await notifications.deny_notifications(ctx.engine, user_id=interaction.user.id, allowed_user_id=claim.claimant_id, now=ctx.now(), idempotency_key=idempotency_key(interaction))
    return Reply(content="このユーザーからの通知を停止しました。")


def _button(action: str, claim_id: int, label: str, style: discord.ButtonStyle) -> discord.ui.Button[discord.ui.View]:
    return discord.ui.Button(label=label, style=style, custom_id=claim_custom_id(action, claim_id))


class ClaimPayButton(discord.ui.DynamicItem[discord.ui.Button[discord.ui.View]], template=re.compile(re.escape(f"{CLAIM_CUSTOM_ID_PREFIX}pay:") + _ID_PATTERN)):
    """承認 (green): the presser pays the claim under the message's component key, so a repeated press replays the first result, and the message becomes 支払い完了 with the 転送ID, or the error with the footer 請求ID: <id>."""

    def __init__(self, claim_id: int) -> None:
        super().__init__(_button("pay", claim_id, "承認", discord.ButtonStyle.green))
        self.claim_id = claim_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction[Any], item: discord.ui.Item[Any], match: re.Match[str], /) -> Self:
        return cls(int(match["claim_id"]))

    async def callback(self, interaction: discord.Interaction[Any]) -> None:
        async def pay() -> Reply:
            return await pay_reply(interaction, self.claim_id, component_idempotency_key(interaction))

        await run_component(interaction, lambda: _keeping_claim_id(interaction, self.claim_id, pay, PAY_OVERRIDES), overrides=PAY_OVERRIDES)


class ClaimDenyButton(discord.ui.DynamicItem[discord.ui.Button[discord.ui.View]], template=re.compile(re.escape(f"{CLAIM_CUSTOM_ID_PREFIX}deny:") + _ID_PATTERN)):
    """拒否 (red): the presser cancels the claim under the message's component key, and the message becomes 請求拒否, or the error with the footer 請求ID: <id>."""

    def __init__(self, claim_id: int) -> None:
        super().__init__(_button("deny", claim_id, "拒否", discord.ButtonStyle.red))
        self.claim_id = claim_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction[Any], item: discord.ui.Item[Any], match: re.Match[str], /) -> Self:
        return cls(int(match["claim_id"]))

    async def callback(self, interaction: discord.Interaction[Any]) -> None:
        await run_component(interaction, lambda: _keeping_claim_id(interaction, self.claim_id, lambda: _deny(interaction, self.claim_id), CANCEL_OVERRIDES), overrides=CANCEL_OVERRIDES)


class ClaimMuteButton(discord.ui.DynamicItem[discord.ui.Button[discord.ui.View]], template=re.compile(re.escape(f"{CLAIM_CUSTOM_ID_PREFIX}mute:") + _ID_PATTERN)):
    """このユーザーからの通知を停止 (grey): the presser stops claim notifications from the claim's claimant, answered ephemerally; the message is unchanged."""

    def __init__(self, claim_id: int) -> None:
        super().__init__(_button("mute", claim_id, "このユーザーからの通知を停止", discord.ButtonStyle.grey))
        self.claim_id = claim_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction[Any], item: discord.ui.Item[Any], match: re.Match[str], /) -> Self:
        return cls(int(match["claim_id"]))

    async def callback(self, interaction: discord.Interaction[Any]) -> None:
        await run_component(interaction, lambda: _mute(interaction, self.claim_id), replace_message=False, ephemeral=True, overrides=_MUTE_OVERRIDES)


CLAIM_BUTTON_CLASSES: Final = (ClaimPayButton, ClaimDenyButton, ClaimMuteButton)


def build_claim_notification(payload: Mapping[str, Any]) -> tuple[discord.Embed, discord.ui.View]:
    """The embed and the persistent view (no timeout; pay, deny and mute buttons) of the direct message for a claim_created payload; raises ValueError for an invalid payload."""
    notice = parse_claim_payload(payload)
    view = discord.ui.View(timeout=None)
    for cls in CLAIM_BUTTON_CLASSES:
        view.add_item(cls(notice.claim_id))
    return claim_notification_embed(notice), view


def claim_notification_message(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The JSON body of a Discord REST create-message request for a claim_created payload: embeds and components with the same embed and custom_ids as build_claim_notification(); raises ValueError for an invalid payload."""
    embed, view = build_claim_notification(payload)
    return {"embeds": [embed.to_dict()], "components": view.to_components()}
