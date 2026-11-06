from __future__ import annotations

import io
import logging
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, cast

import discord
import pytest

from rapidwire.bot.errors import DOMAIN_MESSAGES, GENERIC_MESSAGE, UserInputError
from rapidwire.bot.interaction import (
    Reply,
    component_idempotency_key,
    idempotency_key,
    interaction_log_extra,
    require_guild_id,
    run_command,
    run_component,
    send_error,
)
from rapidwire.core.errors import InsufficientBalance
from rapidwire.db.tables import IDEMPOTENCY_KEY_MAX
from tests.unit.bot.fakes import FakeGuild, FakeInteraction, FakeMessage

SECRET = "internal detail 1234"


def _interaction(**kwargs: Any) -> Any:
    return cast(Any, FakeInteraction(None, **kwargs))


def _http_error() -> discord.HTTPException:
    return discord.HTTPException(cast(Any, SimpleNamespace(status=404, reason="Not Found")), {"message": "Unknown interaction", "code": 10062})


def test_idempotency_key_fits_the_column() -> None:
    interaction = _interaction(interaction_id=2**64 - 1)
    key = idempotency_key(interaction)
    assert key == f"discord:{2**64 - 1}"
    assert len(key) <= IDEMPOTENCY_KEY_MAX


def test_require_guild_id() -> None:
    guild = FakeGuild()
    assert require_guild_id(_interaction(guild=guild)) == guild.id
    with pytest.raises(UserInputError):
        require_guild_id(_interaction())


async def test_run_command_defers_with_thinking_and_sends_the_reply() -> None:
    interaction = _interaction()
    embed = discord.Embed(title="ok")

    async def work() -> Reply:
        assert interaction.response.deferred == {"ephemeral": False, "thinking": True}
        return Reply(embed=embed, content="hello")

    await run_command(interaction, work)
    [sent] = interaction.sent
    assert (sent.via, sent.embeds, sent.content, sent.ephemeral) == ("followup.send", [embed], "hello", False)


async def test_run_command_ephemeral_and_error() -> None:
    interaction = _interaction()

    async def work() -> Reply:
        raise InsufficientBalance(SECRET, execution_id=11)

    await run_command(interaction, work, ephemeral=True)
    assert interaction.response.deferred == {"ephemeral": True, "thinking": True}
    sent = interaction.last
    assert sent.ephemeral
    assert sent.embed.title == "エラー"
    assert sent.embed.description == DOMAIN_MESSAGES["insufficient_balance"]
    assert SECRET not in str(sent.embed.to_dict())


async def test_run_command_overrides(caplog: pytest.LogCaptureFixture) -> None:
    interaction = _interaction()

    async def work() -> None:
        raise InsufficientBalance()

    await run_command(interaction, work, overrides={"insufficient_balance": "残高が不足しているか、承認額 (Allowance) が不足しています。"})
    assert interaction.last.embed.description == "残高が不足しているか、承認額 (Allowance) が不足しています。"


async def test_run_command_unexpected_error_is_generic(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.ERROR, logger="rapidwire.bot")
    interaction = _interaction()

    async def work() -> None:
        raise KeyError(SECRET)

    await run_command(interaction, work)
    assert interaction.last.embed.description == GENERIC_MESSAGE
    assert any(record.exc_info is not None for record in caplog.records if record.name == "rapidwire.bot")


async def test_run_command_when_the_handler_answered_itself() -> None:
    interaction = _interaction()

    async def work() -> None:
        await interaction.followup.send(content="own")

    await run_command(interaction, work)
    assert [entry.content for entry in interaction.sent] == ["own"]


async def test_run_component_replaces_the_pressed_message() -> None:
    message = FakeMessage(content="confirm?", view=object())
    interaction = _interaction(message=message)

    async def work() -> Reply:
        return Reply(embed=discord.Embed(title="done"))

    await run_component(interaction, work)
    assert interaction.response.deferred == {"ephemeral": False, "thinking": False}
    sent = interaction.last
    assert sent.via == "edit_original_response"
    assert sent.view is None
    assert message.view is None
    assert message.embeds[0].title == "done"


async def test_run_component_error_replaces_the_pressed_message() -> None:
    message = FakeMessage(content="confirm?", view=object())
    interaction = _interaction(message=message)

    async def work() -> Reply:
        raise InsufficientBalance()

    await run_component(interaction, work)
    sent = interaction.last
    assert sent.via == "edit_original_response"
    assert (sent.content, sent.view, sent.files) == (None, None, [])
    assert sent.embed.description == DOMAIN_MESSAGES["insufficient_balance"]
    assert message.view is None
    assert message.content is None
    assert message.embeds[0].title == "エラー"


async def test_run_component_error_falls_back_to_a_followup_when_the_edit_fails(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="rapidwire.bot")
    view = object()
    message = FakeMessage(content="confirm?", view=view)
    interaction = _interaction(message=message)

    async def failing_edit(**kwargs: Any) -> None:
        raise _http_error()

    async def work() -> Reply:
        raise InsufficientBalance()

    monkeypatch.setattr(interaction, "edit_original_response", failing_edit)
    await run_component(interaction, work)
    sent = interaction.last
    assert (sent.via, sent.ephemeral, sent.embed.description) == ("followup.send", True, DOMAIN_MESSAGES["insufficient_balance"])
    assert message.view is view
    assert any(record.getMessage() == "could not replace a pressed message with an error" for record in caplog.records)


async def test_run_component_followup_mode_error_is_a_followup() -> None:
    view = object()
    message = FakeMessage(content="confirm?", view=view)
    interaction = _interaction(message=message)

    async def work() -> Reply:
        raise InsufficientBalance()

    await run_component(interaction, work, replace_message=False, ephemeral=False)
    sent = interaction.last
    assert (sent.via, sent.ephemeral) == ("followup.send", False)
    assert message.view is view


async def test_run_component_followup_mode() -> None:
    interaction = _interaction(message=FakeMessage())

    async def work() -> Reply:
        return Reply(content="stopped")

    await run_component(interaction, work, replace_message=False, ephemeral=True)
    assert interaction.response.deferred == {"ephemeral": True, "thinking": True}
    assert (interaction.last.via, interaction.last.ephemeral, interaction.last.content) == ("followup.send", True, "stopped")


def _failing_followup(interaction: Any, failures: int, calls: list[dict[str, Any]]) -> Callable[..., Any]:
    """A followup.send that records each call's keyword arguments with the bytes it read from each file under "uploaded", and raises HTTPException for the first failures calls, then delegates to the fake."""
    original = interaction.followup.send

    async def send(**kwargs: Any) -> Any:
        calls.append({**kwargs, "uploaded": [file.fp.read() for file in kwargs.get("files", [])]})
        if len(calls) <= failures:
            raise _http_error()
        return await original(**kwargs)

    return send


def _messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.name == "rapidwire.bot"]


async def test_run_command_retries_a_refused_reply_without_an_error_embed(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="rapidwire.bot")
    interaction = _interaction()
    calls: list[dict[str, Any]] = []
    file = discord.File(io.BytesIO(b"payload"), filename="x.bin")
    monkeypatch.setattr(interaction.followup, "send", _failing_followup(interaction, 1, calls))

    async def work() -> Reply:
        return Reply(content="done", files=(file,))

    await run_command(interaction, work, ephemeral=True)
    assert [call["ephemeral"] for call in calls] == [True, True]
    [sent] = interaction.sent
    assert (sent.via, sent.content, sent.ephemeral, sent.embeds) == ("followup.send", "done", True, [])
    assert [call["uploaded"] for call in calls] == [[b"payload"], [b"payload"]]
    assert _messages(caplog) == ["could not send the reply of an interaction"]


async def test_run_command_logs_a_reply_that_cannot_be_delivered(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="rapidwire.bot")
    interaction = _interaction()
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(interaction.followup, "send", _failing_followup(interaction, 2, calls))

    async def work() -> Reply:
        return Reply(content="done")

    await run_command(interaction, work)
    assert len(calls) == 2
    assert interaction.sent == []
    assert _messages(caplog) == ["could not send the reply of an interaction"] * 2 + ["the reply of a successful interaction could not be delivered"]


async def test_run_component_sends_the_reply_as_a_followup_when_the_edit_fails(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="rapidwire.bot")
    view = object()
    message = FakeMessage(content="confirm?", view=view)
    interaction = _interaction(message=message)

    async def failing_edit(**kwargs: Any) -> None:
        raise _http_error()

    async def work() -> Reply:
        return Reply(embed=discord.Embed(title="done"))

    monkeypatch.setattr(interaction, "edit_original_response", failing_edit)
    await run_component(interaction, work)
    [sent] = interaction.sent
    assert (sent.via, sent.ephemeral, sent.embed.title) == ("followup.send", True, "done")
    assert message.view is view
    assert _messages(caplog) == ["could not replace a pressed message with its reply"]


async def test_run_component_logs_a_reply_that_cannot_be_delivered(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="rapidwire.bot")
    interaction = _interaction(message=FakeMessage(content="confirm?", view=object()))
    calls: list[dict[str, Any]] = []

    async def failing_edit(**kwargs: Any) -> None:
        raise _http_error()

    async def work() -> Reply:
        return Reply(embed=discord.Embed(title="done"))

    monkeypatch.setattr(interaction, "edit_original_response", failing_edit)
    monkeypatch.setattr(interaction.followup, "send", _failing_followup(interaction, 1, calls))
    await run_component(interaction, work)
    assert [call["ephemeral"] for call in calls] == [True]
    assert interaction.sent == []
    assert _messages(caplog) == ["could not replace a pressed message with its reply", "could not send the reply of an interaction", "the reply of a successful interaction could not be delivered"]


async def test_run_component_followup_mode_retries_a_refused_reply_ephemerally(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="rapidwire.bot")
    interaction = _interaction(message=FakeMessage())
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(interaction.followup, "send", _failing_followup(interaction, 1, calls))

    async def work() -> Reply:
        return Reply(content="stopped")

    await run_component(interaction, work, replace_message=False, ephemeral=False)
    assert [call["ephemeral"] for call in calls] == [False, True]
    [sent] = interaction.sent
    assert (sent.via, sent.content, sent.embeds) == ("followup.send", "stopped", [])
    assert _messages(caplog) == ["could not send the reply of an interaction"]


async def test_send_error_before_any_response() -> None:
    interaction = _interaction()
    await send_error(interaction, UserInputError("だめです。"))
    sent = interaction.last
    assert (sent.via, sent.ephemeral, sent.embed.description) == ("response.send_message", True, "だめです。")


@pytest.mark.parametrize("failure", [_http_error, lambda: discord.InteractionResponded(cast(Any, None))], ids=["http", "responded"])
async def test_send_error_absorbs_a_send_failure(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, failure: Callable[[], Exception]) -> None:
    caplog.set_level(logging.ERROR, logger="rapidwire.bot")
    interaction = _interaction()

    async def failing_send(*args: Any, **kwargs: Any) -> None:
        raise failure()

    monkeypatch.setattr(interaction.response, "send_message", failing_send)
    await send_error(interaction, UserInputError("だめです。"))
    assert interaction.sent == []
    [record] = [record for record in caplog.records if record.name == "rapidwire.bot"]
    assert record.getMessage() == "could not send an error message for an interaction"
    assert record.interaction_id == interaction.id  # type: ignore[attr-defined]


@pytest.mark.parametrize("failure", [_http_error, lambda: discord.InteractionResponded(cast(Any, None))], ids=["http", "responded"])
async def test_run_command_does_not_run_the_work_when_the_defer_fails(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, failure: Callable[[], Exception]) -> None:
    caplog.set_level(logging.WARNING, logger="rapidwire.bot")
    interaction = _interaction()
    ran: list[bool] = []

    async def failing_defer(**kwargs: Any) -> None:
        raise failure()

    async def work() -> Reply:
        ran.append(True)
        return Reply(content="never")

    monkeypatch.setattr(interaction.response, "defer", failing_defer)
    await run_command(interaction, work)
    assert ran == []
    assert interaction.sent == []
    assert any(record.getMessage() == "could not defer an interaction" for record in caplog.records)


async def test_unexpected_error_is_logged_with_the_interaction(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.ERROR, logger="rapidwire.bot")
    interaction = _interaction()
    interaction.command = SimpleNamespace(qualified_name="currency mint")

    async def work() -> None:
        raise KeyError(SECRET)

    await run_command(interaction, work)
    [record] = [record for record in caplog.records if record.getMessage() == "unexpected error in a bot handler"]
    assert (record.command, record.user_id, record.interaction_id) == ("currency mint", interaction.user.id, interaction.id)  # type: ignore[attr-defined]
    assert interaction_log_extra(interaction) == {"interaction_id": interaction.id, "user_id": interaction.user.id, "command": "currency mint"}


def test_empty_reply_is_rejected() -> None:
    with pytest.raises(ValueError, match="Reply needs"):
        Reply()
    with pytest.raises(ValueError):
        Reply(content="", embeds=[])
    assert Reply(content="x").send_kwargs() == {"embeds": [], "content": "x"}


def test_component_idempotency_key() -> None:
    message = FakeMessage(message_id=2**64 - 1)
    first = _interaction(message=message, data={"custom_id": "rapidwire:claim:pay:" + "9" * 20})
    second = _interaction(message=message, data={"custom_id": "rapidwire:claim:pay:" + "9" * 20})
    key = component_idempotency_key(first)
    assert key == f"discord:m{2**64 - 1}:rapidwire:claim:pay:{'9' * 20}"
    assert component_idempotency_key(second) == key
    assert idempotency_key(first) != idempotency_key(second)
    assert len(f"discord:m{2**64 - 1}:" + "x" * 100) <= IDEMPOTENCY_KEY_MAX
    other = _interaction(message=message, data={"custom_id": "rapidwire:claim:cancel:1"})
    assert component_idempotency_key(other) != key
    with pytest.raises(ValueError):
        component_idempotency_key(_interaction(data={"custom_id": "x"}))
    with pytest.raises(ValueError):
        component_idempotency_key(_interaction(message=message))
