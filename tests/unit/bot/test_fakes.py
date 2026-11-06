from __future__ import annotations

import re
from typing import Any

import discord
import pytest
from discord import app_commands

from rapidwire.bot.app import build_bot
from rapidwire.bot.client import RapidWireBot
from rapidwire.bot.embeds import success_embed
from rapidwire.bot.interaction import Reply, run_command, run_component
from rapidwire.config import Settings
from tests.unit.bot.fakes import (
    CHOICES_MAX,
    CONTENT_MAX,
    FakeChannel,
    FakeInteraction,
    FakeMessage,
    autocomplete,
    buttons,
    check_choices,
    check_message,
    invoke,
    press,
    press_dynamic,
)


def _bot() -> RapidWireBot:
    return build_bot(Settings(_env_file=None, db_password="unit-test-password"))


class Confirm(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.pressed: list[int] = []

    @discord.ui.button(label="確認", style=discord.ButtonStyle.success, custom_id="test:confirm")
    async def confirm(self, interaction: discord.Interaction[Any], button: discord.ui.Button[Confirm]) -> None:
        self.pressed.append(interaction.user.id)

        async def work() -> Reply:
            return Reply(embed=success_embed(description="done"), view=None)

        await run_component(interaction, work)

    @discord.ui.button(label="キャンセル", custom_id="test:cancel", disabled=True)
    async def cancel(self, interaction: discord.Interaction[Any], button: discord.ui.Button[Confirm]) -> None:
        raise AssertionError("disabled")


class Counter(discord.ui.DynamicItem[discord.ui.Button[Any]], template=r"test:count:(?P<n>[0-9]+)"):
    def __init__(self, n: int) -> None:
        super().__init__(discord.ui.Button(label=str(n), custom_id=f"test:count:{n}"))
        self.n = n

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction[Any], item: discord.ui.Item[Any], match: re.Match[str], /) -> Counter:
        return cls(int(match["n"]))

    async def callback(self, interaction: discord.Interaction[Any]) -> None:
        async def work() -> Reply:
            return Reply(content=f"count {self.n}")

        await run_component(interaction, work, replace_message=False)


async def test_press_a_view_button_replaces_the_message() -> None:
    view = Confirm()
    message = FakeMessage(view=view)
    interaction = FakeInteraction(_bot(), message=message)
    await press(view, interaction, label="確認")
    assert view.pressed == [interaction.user.id]
    assert interaction.data == {"custom_id": "test:confirm", "component_type": 2}
    assert interaction.last.via == "edit_original_response"
    assert message.embeds[0].description == "done"
    assert message.view is None
    assert [button.label for button in buttons(view)] == ["確認", "キャンセル"]


async def test_press_refuses_disabled_and_ambiguous_buttons() -> None:
    view = Confirm()
    with pytest.raises(AssertionError, match="disabled"):
        await press(view, FakeInteraction(_bot()), custom_id="test:cancel")
    with pytest.raises(AssertionError, match="found 2"):
        await press(view, FakeInteraction(_bot()))


async def test_press_dynamic_item_after_a_restart() -> None:
    bot = _bot()
    bot.add_dynamic_items(Counter)
    interaction = await press_dynamic(bot.dynamic_item_classes, "test:count:42", FakeInteraction(bot))
    assert interaction.last.content == "count 42"
    assert interaction.response.deferred == {"ephemeral": True, "thinking": True}
    with pytest.raises(AssertionError, match="no dynamic item"):
        await press_dynamic(bot.dynamic_item_classes, "test:other", FakeInteraction(bot))


async def test_press_a_dynamic_item_inside_a_view() -> None:
    view = discord.ui.View(timeout=None)
    view.add_item(Counter(7))
    interaction = await press(view, FakeInteraction(_bot()), custom_id="test:count:7")
    assert interaction.last.content == "count 7"


async def test_first_followup_after_a_thinking_defer_takes_the_defer_visibility() -> None:
    interaction = FakeInteraction(_bot())
    await interaction.response.defer(thinking=True, ephemeral=True)
    await interaction.followup.send(content="first", ephemeral=False)
    await interaction.followup.send(content="second", ephemeral=False)
    assert [(entry.content, entry.ephemeral) for entry in interaction.sent] == [("first", True), ("second", False)]
    public = FakeInteraction(_bot())
    await public.response.defer(thinking=True)
    await public.followup.send(content="first", ephemeral=True)
    await public.followup.send(content="second", ephemeral=True)
    assert [entry.ephemeral for entry in public.sent] == [False, True]
    update = FakeInteraction(_bot(), message=FakeMessage())
    await update.response.defer()
    await update.followup.send(content="only", ephemeral=True)
    assert update.last.ephemeral


def _embed_with(**overrides: Any) -> discord.Embed:
    embed = discord.Embed(title=overrides.get("title", "t"), description=overrides.get("description", "d"))
    for index in range(overrides.get("fields", 0)):
        embed.add_field(name=f"f{index}", value="v")
    if "field_name" in overrides or "field_value" in overrides:
        embed.add_field(name=overrides.get("field_name", "n"), value=overrides.get("field_value", "v"))
    if "footer" in overrides:
        embed.set_footer(text=overrides["footer"])
    return embed


@pytest.mark.parametrize(
    ("embeds", "content", "match"),
    [
        ([], "x" * (CONTENT_MAX + 1), "content has"),
        ([discord.Embed(title="t")] * 11, None, "11 embeds"),
        ([_embed_with(title="x" * 257)], None, "title"),
        ([_embed_with(description="x" * 4097)], None, "description"),
        ([_embed_with(fields=26)], None, "26 fields"),
        ([_embed_with(field_name="x" * 257)], None, "field name"),
        ([_embed_with(field_value="x" * 1025)], None, "value has 1025"),
        ([_embed_with(footer="x" * 2049)], None, "footer"),
        ([_embed_with(description="x" * 4000), _embed_with(description="x" * 2000)], None, "more than 6000"),
    ],
    ids=["content", "embed count", "title", "description", "field count", "field name", "field value", "footer", "total"],
)
async def test_sends_beyond_discord_limits_are_refused(embeds: list[discord.Embed], content: str | None, match: str) -> None:
    interaction = FakeInteraction(_bot(), message=FakeMessage())
    with pytest.raises(AssertionError, match=match):
        await interaction.response.send_message(content, embeds=embeds)
    assert interaction.sent == [] and not interaction.response.is_done()
    await interaction.response.defer()
    for attempt in (interaction.followup.send(content, embeds=embeds), interaction.edit_original_response(content=content, embeds=embeds), FakeChannel().send(content, embeds=embeds)):
        with pytest.raises(AssertionError, match=match):
            await attempt
    assert interaction.sent == []


def test_sends_at_discord_limits_are_accepted() -> None:
    check_message("x" * CONTENT_MAX, [_embed_with(title="x" * 256, description="x" * 4096, fields=24, field_value="x" * 1024)])
    check_message(None, [discord.Embed(title="t")] * 10)


async def test_autocomplete_limits() -> None:
    bot = _bot()

    class Demo(app_commands.Group):
        @app_commands.command(name="pick", description="p")
        async def pick(self, interaction: discord.Interaction[Any], text: str) -> None:
            pass

        @pick.autocomplete("text")
        async def pick_text(self, interaction: discord.Interaction[Any], current: str) -> list[app_commands.Choice[str]]:
            count, size = (int(part) for part in current.split(","))
            return [app_commands.Choice(name="n" * size, value=str(index)) for index in range(count)]

    bot.tree.add_command(Demo(name="demo", description="d"))
    interaction = FakeInteraction(bot)
    assert len(await autocomplete(bot.tree, "demo pick", "text", interaction, f"{CHOICES_MAX},100")) == CHOICES_MAX
    assert interaction.type is discord.InteractionType.autocomplete
    with pytest.raises(AssertionError, match="26 autocomplete choices"):
        await autocomplete(bot.tree, "demo pick", "text", FakeInteraction(bot), "26,1")
    with pytest.raises(AssertionError, match="choice name"):
        await autocomplete(bot.tree, "demo pick", "text", FakeInteraction(bot), "1,101")
    with pytest.raises(AssertionError, match="choice value"):
        check_choices([app_commands.Choice(name="n", value="v" * 101)])
    responder = FakeInteraction(bot)
    with pytest.raises(AssertionError, match="autocomplete choices"):
        await responder.response.autocomplete([app_commands.Choice(name="n", value="v")] * 26)
    assert not responder.response.is_done()


async def test_interaction_type_follows_what_is_simulated() -> None:
    bot = _bot()
    assert FakeInteraction(bot).type is discord.InteractionType.application_command
    view = Confirm()
    pressed = await press(view, FakeInteraction(bot, message=FakeMessage(view=view)), label="確認")
    assert pressed.type is discord.InteractionType.component
    bot.add_dynamic_items(Counter)
    dynamic = await press_dynamic(bot.dynamic_item_classes, "test:count:1", FakeInteraction(bot))
    assert dynamic.type is discord.InteractionType.component


async def test_invoke_and_autocomplete_bind_group_methods() -> None:
    bot = _bot()

    class Demo(app_commands.Group):
        @app_commands.command(name="echo", description="e")
        async def echo(self, interaction: discord.Interaction[Any], text: str) -> None:
            async def work() -> Reply:
                return Reply(content=f"{self.name}:{text}")

            await run_command(interaction, work, ephemeral=True)

        @echo.autocomplete("text")
        async def echo_text(self, interaction: discord.Interaction[Any], current: str) -> list[app_commands.Choice[str]]:
            return [app_commands.Choice(name=current.upper(), value=current.upper())]

    bot.tree.add_command(Demo(name="demo", description="d"))
    interaction = await invoke(bot.tree, "demo echo", FakeInteraction(bot), text="hi")
    assert interaction.last.content == "demo:hi"
    assert interaction.last.ephemeral
    choices = await autocomplete(bot.tree, "demo echo", "text", FakeInteraction(bot), "ab")
    assert [choice.value for choice in choices] == ["AB"]
