from __future__ import annotations

from typing import Any

import discord
from discord import app_commands
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.bot.app import build_bot
from rapidwire.bot.client import RapidWireBot
from rapidwire.bot.views.claim import CLAIM_BUTTON_CLASSES, CLAIM_PAYLOAD_KEYS, build_claim_notification, claim_custom_id, claim_notification_message
from rapidwire.config import Settings
from rapidwire.core import claims, notifications
from rapidwire.core.permissions import InMemoryGuildInfo
from rapidwire.core.types import ClaimStatus, OutboxKind
from rapidwire.db.tables import outbox
from tests.integration.bot.conftest import MakeInteraction
from tests.integration.factories import balance_of, create_currency, mint
from tests.unit.bot.fakes import FakeInteraction, FakeMessage, FakeUser, buttons, invoke, press, press_dynamic

GUILD = 7_300_001
NO_CURRENCY_GUILD = 7_300_002
CLAIMANT = 7_400_001
PAYER = 7_400_002
STRANGER = 7_400_003
UNIT = 1000


def _error(interaction: FakeInteraction) -> str:
    embed = interaction.last.embed
    assert embed.title == "エラー", embed.description
    return embed.description or ""


async def _seed(db: AsyncEngine) -> None:
    await create_currency(db, currency_id=GUILD, symbol="RAPID", name="Rapid")
    await mint(db, GUILD, PAYER, 100 * UNIT)


async def _outbox_payloads(db: AsyncEngine) -> list[dict[str, Any]]:
    async with db.connect() as conn:
        rows = (await conn.execute(select(outbox.c.kind, outbox.c.payload).order_by(outbox.c.outbox_id))).all()
    return [row.payload for row in rows if row.kind == OutboxKind.CLAIM_CREATED]


async def test_claim_create(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    await _seed(db)
    payer = FakeUser(PAYER)
    interaction = await invoke(bot.tree, "claim create", make_interaction(user_id=CLAIMANT, guild_id=GUILD), user=payer, amount="12.5", description="lunch")
    embed = interaction.last.embed
    assert (embed.title, embed.description) == ("請求作成完了", f"<@{PAYER}> への請求を作成しました。")
    assert embed.fields[0].name == "請求ID"
    claim_id = int((embed.fields[0].value or "").strip("`"))
    claim = await claims.get_claim(db, claim_id)
    assert (claim.claimant_id, claim.payer_id, claim.currency_id, claim.amount, claim.description) == (CLAIMANT, PAYER, GUILD, 12_500, "lunch")
    assert await _outbox_payloads(db) == []


async def test_claim_create_errors(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    await _seed(db)
    payer = FakeUser(PAYER)
    assert _error(await invoke(bot.tree, "claim create", make_interaction(user_id=CLAIMANT), user=payer, amount="1", description=None)) == "このコマンドはサーバー内でのみ使用できます。"
    no_currency = make_interaction(user_id=CLAIMANT, guild_id=NO_CURRENCY_GUILD)
    assert _error(await invoke(bot.tree, "claim create", no_currency, user=payer, amount="1", description=None)) == "このサーバーには通貨が存在しません。"
    too_long = make_interaction(user_id=CLAIMANT, guild_id=GUILD)
    assert _error(await invoke(bot.tree, "claim create", too_long, user=payer, amount="1", description="x" * 51)) == "請求の説明は 50 文字以内で指定してください。"
    to_self = make_interaction(user_id=CLAIMANT, guild_id=GUILD)
    assert _error(await invoke(bot.tree, "claim create", to_self, user=FakeUser(CLAIMANT), amount="1", description=None)) == "自分自身に請求することはできません。"
    zero = make_interaction(user_id=CLAIMANT, guild_id=GUILD)
    assert _error(await invoke(bot.tree, "claim create", zero, user=payer, amount="0", description=None)) == "金額は 0.001 以上を指定してください。"


async def test_claim_list(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    await _seed(db)
    empty = (await invoke(bot.tree, "claim list", make_interaction(user_id=STRANGER), page=1)).last.embed
    assert (empty.title, empty.description) == ("請求一覧", "関連する請求はありません。")
    first = await claims.create_claim(db, claimant_id=CLAIMANT, payer_id=PAYER, currency_id=GUILD, amount=1500, description="a *b*")
    second = await claims.create_claim(db, claimant_id=PAYER, payer_id=CLAIMANT, currency_id=GUILD, amount=2 * UNIT)
    await claims.cancel_claim(db, claim_id=second.claim_id, user_id=CLAIMANT)
    for user_id in (CLAIMANT, PAYER):
        embed = (await invoke(bot.tree, "claim list", make_interaction(user_id=user_id), page=1)).last.embed
        assert embed.title == "請求一覧 (ページ 1/1)"
        assert [(field.name, field.value) for field in embed.fields] == [
            (f"ID: {second.claim_id} | CANCELED | 2.000 RAPID", f"請求者: <@{PAYER}>\n支払者: <@{CLAIMANT}>\n説明: N/A"),
            (f"ID: {first.claim_id} | PENDING | 1.500 RAPID", f"請求者: <@{CLAIMANT}>\n支払者: <@{PAYER}>\n説明: a \\*b\\*"),
        ]
    assert _error(await invoke(bot.tree, "claim list", make_interaction(user_id=CLAIMANT), page=2)) == "指定されたページ (2) は存在しません。全 1 ページです。"


async def test_claim_list_filters_by_role(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    await _seed(db)
    made = await claims.create_claim(db, claimant_id=CLAIMANT, payer_id=PAYER, currency_id=GUILD, amount=1500)
    against = await claims.create_claim(db, claimant_id=PAYER, payer_id=CLAIMANT, currency_id=GUILD, amount=2 * UNIT)
    claimant_role = app_commands.Choice(name="請求者として", value="claimant")
    payer_role = app_commands.Choice(name="支払者として", value="payer")
    for role, expected in ((claimant_role, made), (payer_role, against)):
        embed = (await invoke(bot.tree, "claim list", make_interaction(user_id=CLAIMANT), page=1, role=role)).last.embed
        assert embed.title == "請求一覧 (ページ 1/1)"
        assert [str(field.name).split(" | ")[0] for field in embed.fields] == [f"ID: {expected.claim_id}"]
    both = (await invoke(bot.tree, "claim list", make_interaction(user_id=CLAIMANT), page=1, role=None)).last.embed
    assert [str(field.name).split(" | ")[0] for field in both.fields] == [f"ID: {against.claim_id}", f"ID: {made.claim_id}"]
    empty = (await invoke(bot.tree, "claim list", make_interaction(user_id=STRANGER), page=1, role=payer_role)).last.embed
    assert (empty.title, empty.description) == ("請求一覧", "関連する請求はありません。")


async def test_claim_pay_and_cancel_commands(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    await _seed(db)
    created = await claims.create_claim(db, claimant_id=CLAIMANT, payer_id=PAYER, currency_id=GUILD, amount=3 * UNIT)
    assert _error(await invoke(bot.tree, "claim pay", make_interaction(user_id=CLAIMANT), claim_id=created.claim_id)) == "この請求を支払えるのは支払者だけです。"
    embed = (await invoke(bot.tree, "claim pay", make_interaction(user_id=PAYER), claim_id=created.claim_id)).last.embed
    assert (embed.title, embed.description) == ("支払い完了", f"請求ID `{created.claim_id}` の支払いが完了しました。")
    assert embed.fields[0].name == "転送ID"
    assert await balance_of(db, CLAIMANT, GUILD) == 3 * UNIT
    assert _error(await invoke(bot.tree, "claim pay", make_interaction(user_id=PAYER), claim_id=created.claim_id)) == "この請求は既に支払い済みか、キャンセルされています。"
    assert _error(await invoke(bot.tree, "claim pay", make_interaction(user_id=PAYER), claim_id=987_654)) == "請求が見つかりませんでした。"

    other = await claims.create_claim(db, claimant_id=CLAIMANT, payer_id=PAYER, currency_id=GUILD, amount=1)
    assert _error(await invoke(bot.tree, "claim cancel", make_interaction(user_id=STRANGER), claim_id=other.claim_id)) == "この請求をキャンセルできるのは請求者と支払者だけです。"
    embed = (await invoke(bot.tree, "claim cancel", make_interaction(user_id=CLAIMANT), claim_id=other.claim_id)).last.embed
    assert (embed.title, embed.description) == ("キャンセル完了", f"請求ID `{other.claim_id}` をキャンセルしました。")
    assert (await claims.get_claim(db, other.claim_id)).status is ClaimStatus.CANCELED


async def _notified_claim(db: AsyncEngine, amount: int = 4 * UNIT) -> dict[str, Any]:
    await notifications.allow_notifications(db, user_id=PAYER, allowed_user_id=CLAIMANT)
    await claims.create_claim(db, claimant_id=CLAIMANT, payer_id=PAYER, currency_id=GUILD, amount=amount, description="dinner")
    (payload,) = await _outbox_payloads(db)
    return payload


def _dm(bot: RapidWireBot, user_id: int, payload: dict[str, Any]) -> tuple[FakeInteraction, discord.ui.View]:
    embed, view = build_claim_notification(payload)
    message = FakeMessage(embeds=[embed], view=view)
    return FakeInteraction(bot, user=FakeUser(user_id), message=message), view


async def test_outbox_payload_builds_the_notification(db: AsyncEngine, bot: RapidWireBot) -> None:
    await _seed(db)
    payload = await _notified_claim(db)
    assert frozenset(payload) == CLAIM_PAYLOAD_KEYS
    embed, _ = build_claim_notification(payload)
    assert [(field.name, field.value) for field in embed.fields] == [("請求者", f"<@{CLAIMANT}>"), ("金額", "`4.000 RAPID`"), ("説明", "dinner")]
    message = claim_notification_message(payload)
    assert [component["custom_id"] for component in message["components"][0]["components"]] == [claim_custom_id(action, int(payload["claim_id"])) for action in ("pay", "deny", "mute")]


async def test_pay_button(db: AsyncEngine, bot: RapidWireBot) -> None:
    await _seed(db)
    payload = await _notified_claim(db)
    claim_id = int(payload["claim_id"])

    interaction, view = _dm(bot, STRANGER, payload)
    await press(view, interaction, label="承認")
    assert interaction.last.via == "edit_original_response"
    assert _error(interaction) == "この請求を支払えるのは支払者だけです。"
    assert interaction.last.embed.footer.text == f"請求ID: {claim_id}"
    assert interaction.message is not None and interaction.message.view is None
    assert (await claims.get_claim(db, claim_id)).status is ClaimStatus.PENDING

    interaction, view = _dm(bot, PAYER, payload)
    await press(view, interaction, label="承認")
    assert interaction.last.via == "edit_original_response"
    embed = interaction.last.embed
    assert (embed.title, embed.description) == ("支払い完了", f"請求ID `{claim_id}` の支払いが完了しました。")
    assert embed.fields[0].name == "転送ID"
    assert interaction.last.view is None
    assert interaction.message is not None and interaction.message.view is None
    assert await balance_of(db, CLAIMANT, GUILD) == 4 * UNIT
    assert (await claims.get_claim(db, claim_id)).status is ClaimStatus.PAID


async def test_a_strangers_press_does_not_block_the_payer_on_the_same_message(db: AsyncEngine, bot: RapidWireBot) -> None:
    await _seed(db)
    payload = await _notified_claim(db)
    claim_id = int(payload["claim_id"])
    embed, view = build_claim_notification(payload)
    message = FakeMessage(embeds=[embed], view=view)
    stranger = FakeInteraction(bot, user=FakeUser(STRANGER), message=message)
    payer = FakeInteraction(bot, user=FakeUser(PAYER), message=message)
    await press(view, stranger, label="承認")
    assert _error(stranger) == "この請求を支払えるのは支払者だけです。"
    await press(view, payer, label="承認")
    paid = payer.last.embed
    assert (payer.last.via, paid.title, paid.description) == ("edit_original_response", "支払い完了", f"請求ID `{claim_id}` の支払いが完了しました。")
    assert message.embeds[0].title == "支払い完了"
    assert await balance_of(db, CLAIMANT, GUILD) == 4 * UNIT
    assert await balance_of(db, PAYER, GUILD) == 96 * UNIT
    assert (await claims.get_claim(db, claim_id)).status is ClaimStatus.PAID


async def test_pay_button_with_insufficient_balance(db: AsyncEngine, bot: RapidWireBot) -> None:
    await _seed(db)
    payload = await _notified_claim(db, amount=1000 * UNIT)
    interaction, view = _dm(bot, PAYER, payload)
    claim_id = int(payload["claim_id"])
    await press(view, interaction, custom_id=claim_custom_id("pay", claim_id))
    assert _error(interaction) == "残高が不足しています。"
    embed = interaction.last.embed
    assert embed.fields[-1].name == "実行ID"
    assert embed.footer.text == f"請求ID: {claim_id}"
    assert interaction.message is not None and interaction.message.view is None and interaction.message.embeds == [embed]
    assert (await claims.get_claim(db, claim_id)).status is ClaimStatus.PENDING

    await mint(db, GUILD, PAYER, 1000 * UNIT)
    paid = (await invoke(bot.tree, "claim pay", FakeInteraction(bot, user=FakeUser(PAYER)), claim_id=claim_id)).last.embed
    assert paid.title == "支払い完了"
    assert (await claims.get_claim(db, claim_id)).status is ClaimStatus.PAID


async def test_double_press_of_pay_replays_the_payment(db: AsyncEngine, bot: RapidWireBot) -> None:
    await _seed(db)
    payload = await _notified_claim(db)
    claim_id = int(payload["claim_id"])
    embed, view = build_claim_notification(payload)
    message = FakeMessage(embeds=[embed], view=view)
    first = FakeInteraction(bot, user=FakeUser(PAYER), message=message)
    second = FakeInteraction(bot, user=FakeUser(PAYER), message=message)
    assert first.id != second.id
    await press(view, first, label="承認")
    await press(view, second, label="承認")
    for interaction in (first, second):
        assert (interaction.last.via, interaction.last.embed.title) == ("edit_original_response", "支払い完了")
    assert first.last.embed.fields[0].value == second.last.embed.fields[0].value
    assert message.embeds[0].title == "支払い完了"
    assert await balance_of(db, CLAIMANT, GUILD) == 4 * UNIT
    assert await balance_of(db, PAYER, GUILD) == 96 * UNIT
    assert (await claims.get_claim(db, claim_id)).status is ClaimStatus.PAID


async def test_double_press_of_deny_replays_the_cancel(db: AsyncEngine, bot: RapidWireBot) -> None:
    await _seed(db)
    payload = await _notified_claim(db)
    embed, view = build_claim_notification(payload)
    message = FakeMessage(embeds=[embed], view=view)
    for _ in range(2):
        interaction = FakeInteraction(bot, user=FakeUser(PAYER), message=message)
        await press(view, interaction, label="拒否")
        assert interaction.last.embed.title == "請求拒否"
    assert (await claims.get_claim(db, int(payload["claim_id"]))).status is ClaimStatus.CANCELED


async def test_deny_button(db: AsyncEngine, bot: RapidWireBot) -> None:
    await _seed(db)
    payload = await _notified_claim(db)
    claim_id = int(payload["claim_id"])
    interaction, view = _dm(bot, STRANGER, payload)
    await press(view, interaction, label="拒否")
    assert _error(interaction) == "この請求をキャンセルできるのは請求者と支払者だけです。"
    assert interaction.last.embed.footer.text == f"請求ID: {claim_id}"
    interaction, view = _dm(bot, PAYER, payload)
    await press(view, interaction, label="拒否")
    embed = interaction.last.embed
    assert (embed.title, embed.description, embed.colour) == ("請求拒否", f"請求ID `{claim_id}` を拒否しました。", discord.Colour.green())
    assert (await claims.get_claim(db, claim_id)).status is ClaimStatus.CANCELED
    interaction, view = _dm(bot, PAYER, payload)
    await press(view, interaction, label="承認")
    assert _error(interaction) == "この請求は既に支払い済みか、キャンセルされています。"


async def test_mute_button(db: AsyncEngine, bot: RapidWireBot) -> None:
    await _seed(db)
    payload = await _notified_claim(db)
    assert await notifications.is_notification_allowed(db, PAYER, CLAIMANT)
    interaction, view = _dm(bot, PAYER, payload)
    await press(view, interaction, label="このユーザーからの通知を停止")
    assert interaction.response.deferred == {"ephemeral": True, "thinking": True}
    assert (interaction.last.via, interaction.last.content, interaction.last.ephemeral) == ("followup.send", "このユーザーからの通知を停止しました。", True)
    assert interaction.message is not None and interaction.message.edits == []
    assert not await notifications.is_notification_allowed(db, PAYER, CLAIMANT)
    assert (await claims.get_claim(db, int(payload["claim_id"]))).status is ClaimStatus.PENDING

    await claims.create_claim(db, claimant_id=CLAIMANT, payer_id=PAYER, currency_id=GUILD, amount=1)
    assert len(await _outbox_payloads(db)) == 1


async def test_buttons_survive_a_restart(db: AsyncEngine, bot_settings: Settings, guild_info: InMemoryGuildInfo) -> None:
    await _seed(db)
    payload = await _notified_claim(db)
    claim_id = int(payload["claim_id"])
    _, view = build_claim_notification(payload)
    custom_ids = [button.custom_id for button in buttons(view)]
    del view

    restarted = build_bot(bot_settings, engine=db, guild_info=guild_info)
    # discord.py routes component interactions through the connection's view store, so the presses go through the classes registered there.
    stored = restarted._connection._view_store._dynamic_items
    store_classes = tuple(stored.values())
    for custom_id, expected in zip(custom_ids, CLAIM_BUTTON_CLASSES, strict=True):
        assert custom_id is not None
        assert [cls for pattern, cls in stored.items() if pattern.fullmatch(custom_id)] == [expected]

    mute = FakeInteraction(restarted, user=FakeUser(PAYER), message=FakeMessage())
    await press_dynamic(store_classes, claim_custom_id("mute", claim_id), mute)
    assert mute.last.content == "このユーザーからの通知を停止しました。"

    pay = FakeInteraction(restarted, user=FakeUser(PAYER), message=FakeMessage())
    assert custom_ids[0] is not None
    await press_dynamic(store_classes, custom_ids[0], pay)
    assert pay.last.embed.title == "支払い完了"
    assert (await claims.get_claim(db, claim_id)).status is ClaimStatus.PAID

    deny = FakeInteraction(restarted, user=FakeUser(PAYER), message=FakeMessage())
    assert custom_ids[1] is not None
    await press_dynamic(store_classes, custom_ids[1], deny)
    assert _error(deny) == "この請求は既に支払い済みか、キャンセルされています。"
    assert deny.last.embed.footer.text == f"請求ID: {claim_id}"


async def test_unknown_claim_button(db: AsyncEngine, bot: RapidWireBot) -> None:
    interaction = FakeInteraction(bot, user=FakeUser(PAYER), message=FakeMessage())
    await press_dynamic(bot.dynamic_item_classes, claim_custom_id("mute", 9_999_999_999_999_999_999), interaction)
    assert _error(interaction) == "請求が見つかりませんでした。"
    interaction = FakeInteraction(bot, user=FakeUser(PAYER), message=FakeMessage())
    await press_dynamic(bot.dynamic_item_classes, claim_custom_id("pay", 12345), interaction)
    assert _error(interaction) == "請求が見つかりませんでした。"
