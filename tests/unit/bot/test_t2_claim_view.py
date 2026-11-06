from __future__ import annotations

from typing import Any

import discord
import pytest

from rapidwire.bot.app import build_bot
from rapidwire.bot.views.claim import (
    CLAIM_BUTTON_CLASSES,
    ClaimDenyButton,
    ClaimMuteButton,
    ClaimPayButton,
    build_claim_notification,
    claim_custom_id,
    claim_notification_message,
    parse_claim_payload,
)
from rapidwire.config import Settings
from rapidwire.core.validation import BIGINT_MAX
from tests.unit.bot.fakes import buttons

CLAIM_ID = 2**62 + 5


def _payload(**changes: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "claim_id": str(CLAIM_ID),
        "claimant_id": "111",
        "payer_id": "222",
        "currency_id": "333",
        "currency_symbol": "RAPID",
        "amount": "1234567",
        "description": "lunch @everyone **bold**",
    }
    payload.update(changes)
    return payload


def test_payload_is_parsed_exactly() -> None:
    notice = parse_claim_payload(_payload())
    assert (notice.claim_id, notice.claimant_id, notice.payer_id, notice.currency_id, notice.amount) == (CLAIM_ID, 111, 222, 333, 1_234_567)
    assert notice.currency_symbol == "RAPID"
    assert notice.description == "lunch @everyone **bold**"
    assert parse_claim_payload(_payload(description=None)).description is None


@pytest.mark.parametrize(
    "payload",
    [
        {key: value for key, value in _payload().items() if key != "description"},
        {**_payload(), "extra": "1"},
        _payload(claim_id=5),
        _payload(claim_id="0"),
        _payload(claim_id="05"),
        _payload(claim_id=str(BIGINT_MAX + 1)),
        _payload(amount="1.5"),
        _payload(amount="0"),
        _payload(amount=str(10**38)),
        _payload(payer_id="-1"),
        _payload(currency_symbol=""),
        _payload(currency_symbol="TOOLONGSYM"),
        _payload(currency_symbol=None),
        _payload(description=""),
        _payload(description="x" * 51),
        _payload(description=3),
    ],
)
def test_invalid_payloads_raise_value_error(payload: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="claim_created payload"):
        parse_claim_payload(payload)
    with pytest.raises(ValueError, match="claim_created payload"):
        claim_notification_message(payload)


def test_embed_matches_the_earlier_bot() -> None:
    embed, _ = build_claim_notification(_payload())
    assert embed.title == "請求の通知"
    assert embed.colour == discord.Colour.blue()
    assert [(field.name, field.value, field.inline) for field in embed.fields] == [
        ("請求者", "<@111>", False),
        ("金額", "`1,234.567 RAPID`", False),
        ("説明", "lunch @​everyone \\*\\*bold\\*\\*", False),
    ]
    assert embed.footer.text == f"請求ID: {CLAIM_ID}"
    embed, _ = build_claim_notification(_payload(description=None))
    assert [field.name for field in embed.fields] == ["請求者", "金額"]


def test_view_buttons_and_custom_id_round_trip() -> None:
    _, view = build_claim_notification(_payload())
    assert view.timeout is None
    assert view.is_persistent()
    found = buttons(view)
    assert [(button.label, button.style) for button in found] == [
        ("承認", discord.ButtonStyle.green),
        ("拒否", discord.ButtonStyle.red),
        ("このユーザーからの通知を停止", discord.ButtonStyle.grey),
    ]
    assert [button.custom_id for button in found] == [f"rw:claim:pay:{CLAIM_ID}", f"rw:claim:deny:{CLAIM_ID}", f"rw:claim:mute:{CLAIM_ID}"]
    for cls, button in zip(CLAIM_BUTTON_CLASSES, found, strict=True):
        assert button.custom_id is not None
        match = cls.__discord_ui_compiled_template__.fullmatch(button.custom_id)
        assert match is not None
        assert int(match["claim_id"]) == CLAIM_ID
        assert all(other.__discord_ui_compiled_template__.fullmatch(button.custom_id) is None for other in CLAIM_BUTTON_CLASSES if other is not cls)


async def test_from_custom_id_rebuilds_the_item() -> None:
    for cls, action in ((ClaimPayButton, "pay"), (ClaimDenyButton, "deny"), (ClaimMuteButton, "mute")):
        custom_id = claim_custom_id(action, 42)
        match = cls.__discord_ui_compiled_template__.fullmatch(custom_id)
        assert match is not None
        item = await cls.from_custom_id(Any, discord.ui.Button[Any](custom_id=custom_id), match)  # type: ignore[arg-type]
        assert item.claim_id == 42
        assert item.custom_id == custom_id


@pytest.mark.parametrize("custom_id", ["rw:claim:pay:0", "rw:claim:pay:01", "rw:claim:pay:", "rw:claim:pay:12x", "rw:claim:other:1", "rw:claim:pay:" + "9" * 20])
def test_templates_reject_other_custom_ids(custom_id: str) -> None:
    assert all(cls.__discord_ui_compiled_template__.fullmatch(custom_id) is None for cls in CLAIM_BUTTON_CLASSES)


def test_rest_json_equals_the_view() -> None:
    message = claim_notification_message(_payload())
    embed, view = build_claim_notification(_payload())
    assert set(message) == {"embeds", "components"}
    assert message["embeds"] == [embed.to_dict()]
    assert message["components"] == view.to_components()
    (row,) = message["components"]
    assert row["type"] == discord.ComponentType.action_row.value
    custom_ids = [component["custom_id"] for component in row["components"]]
    assert custom_ids == [f"rw:claim:{action}:{CLAIM_ID}" for action in ("pay", "deny", "mute")]
    for cls, custom_id in zip(CLAIM_BUTTON_CLASSES, custom_ids, strict=True):
        assert cls.__discord_ui_compiled_template__.fullmatch(custom_id) is not None
    assert [component["style"] for component in row["components"]] == [discord.ButtonStyle.green.value, discord.ButtonStyle.red.value, discord.ButtonStyle.grey.value]
    assert message["embeds"][0]["title"] == "請求の通知"


def test_claim_setup_registers_the_dynamic_items_in_the_view_store() -> None:
    bot = build_bot(Settings(_env_file=None, db_password="unit-test-password"))
    stored = bot._connection._view_store._dynamic_items
    for cls in CLAIM_BUTTON_CLASSES:
        assert stored[cls.__discord_ui_compiled_template__] is cls
