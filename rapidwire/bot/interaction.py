"""The common handler wrappers: every slash command runs its work through run_command() and every button through run_component(), which defer the interaction, send the work's Reply, and turn any exception from the work into the presenter's error embed; a Reply Discord refuses is retried and logged, never reported as an error."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

import discord

from rapidwire.bot.errors import UserInputError, present_error

__all__ = [
    "IDEMPOTENCY_KEY_PREFIX",
    "Reply",
    "Work",
    "component_idempotency_key",
    "idempotency_key",
    "interaction_log_extra",
    "require_guild_id",
    "run_command",
    "run_component",
    "send_error",
]

log = logging.getLogger("rapidwire.bot")

IDEMPOTENCY_KEY_PREFIX: Final = "discord:"

# What a failed interaction response raises: HTTPException for Discord's refusal or an expired token, ClientException (InteractionResponded) when it was already answered.
_SEND_ERRORS: Final = (discord.HTTPException, discord.ClientException)


@dataclass(frozen=True)
class Reply:
    """What a handler answers: content, embeds (embed first, then embeds), a view and files, at least one of them set (ValueError otherwise); when it replaces a message, view None removes the message's components."""

    embed: discord.Embed | None = None
    content: str | None = None
    embeds: Sequence[discord.Embed] = ()
    view: discord.ui.View | None = None
    files: Sequence[discord.File] = ()

    def __post_init__(self) -> None:
        if self.embed is None and not self.content and not self.embeds and self.view is None and not self.files:
            raise ValueError("a Reply needs content, an embed, a view or a file")

    def all_embeds(self) -> list[discord.Embed]:
        return ([self.embed] if self.embed is not None else []) + list(self.embeds)

    def send_kwargs(self) -> dict[str, Any]:
        """Keyword arguments for Webhook.send and InteractionResponse.send_message, leaving out what is not set."""
        kwargs: dict[str, Any] = {"embeds": self.all_embeds()}
        if self.content is not None:
            kwargs["content"] = self.content
        if self.view is not None:
            kwargs["view"] = self.view
        if self.files:
            kwargs["files"] = list(self.files)
        return kwargs

    def edit_kwargs(self) -> dict[str, Any]:
        """Keyword arguments for Interaction.edit_original_response, replacing the content, embeds, components and attachments of the message."""
        return {"content": self.content, "embeds": self.all_embeds(), "view": self.view, "attachments": list(self.files)}


# The work of a handler: it returns the Reply to send, or None when it has already answered through the interaction itself.
type Work = Callable[[], Awaitable[Reply | None]]


def idempotency_key(interaction: discord.Interaction[Any]) -> str:
    """The idempotency key discord:<interaction id>, unique to one interaction: it stops the operation of a single slash command or press from being applied twice, while every new press of a button is a new interaction with a new key."""
    return f"{IDEMPOTENCY_KEY_PREFIX}{interaction.id}"


def component_idempotency_key(interaction: discord.Interaction[Any]) -> str:
    """The idempotency key discord:m<message id>:<custom_id> of a one-shot button press, the same for every press of that button on that message, so a repeated press by the same initiator replays the first result; ValueError when the interaction has no message or custom_id."""
    message = interaction.message
    data: Mapping[str, Any] = interaction.data or {}
    custom_id = data.get("custom_id")
    if message is None or not isinstance(custom_id, str) or not custom_id:
        raise ValueError("a component idempotency key needs the pressed message and the component's custom_id")
    return f"{IDEMPOTENCY_KEY_PREFIX}m{message.id}:{custom_id}"


def require_guild_id(interaction: discord.Interaction[Any]) -> int:
    """The ID of the guild the interaction comes from, raising UserInputError in direct messages."""
    if interaction.guild_id is None:
        raise UserInputError("このコマンドはサーバー内でのみ使用できます。")
    return interaction.guild_id


def interaction_log_extra(interaction: discord.Interaction[Any]) -> dict[str, Any]:
    """Log fields identifying an interaction: its ID, the user's ID and the qualified name of its command (None for components)."""
    command = getattr(interaction, "command", None)
    user = getattr(interaction, "user", None)
    return {
        "interaction_id": interaction.id,
        "user_id": getattr(user, "id", None),
        "command": getattr(command, "qualified_name", None),
    }


async def send_error(interaction: discord.Interaction[Any], error: BaseException, *, ephemeral: bool = True, overrides: Mapping[str, str] | None = None) -> None:
    """Show the presenter's embed for error: as the interaction's response when nothing was sent yet, otherwise as a followup; a failure to send is logged and not raised."""
    embed = present_error(error, overrides=overrides, log_extra=interaction_log_extra(interaction))
    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=ephemeral)
    except _SEND_ERRORS:
        log.exception("could not send an error message for an interaction", extra={"interaction_id": interaction.id})


async def _defer(interaction: discord.Interaction[Any], **kwargs: Any) -> bool:
    if interaction.response.is_done():
        return True
    try:
        await interaction.response.defer(**kwargs)
    except _SEND_ERRORS:
        # The interaction token expired or was already answered elsewhere, so nothing can be sent for it.
        log.warning("could not defer an interaction", exc_info=True, extra={"interaction_id": interaction.id})
        return False
    return True


async def _followup(interaction: discord.Interaction[Any], reply: Reply, *, ephemeral: bool) -> bool:
    """Send reply as a followup with its files rewound to their start; False when Discord refused it, which is logged."""
    for file in reply.files:
        file.reset()
    try:
        await interaction.followup.send(ephemeral=ephemeral, **reply.send_kwargs())
    except _SEND_ERRORS:
        log.warning("could not send the reply of an interaction", exc_info=True, extra=interaction_log_extra(interaction))
        return False
    return True


async def _deliver_followup(interaction: discord.Interaction[Any], reply: Reply, *, ephemeral: bool, fallback_ephemeral: bool) -> None:
    """Send the Reply of an operation that succeeded, trying once more with fallback_ephemeral when Discord refuses it; a delivery failure never shows an error embed, because the operation itself was applied."""
    if await _followup(interaction, reply, ephemeral=ephemeral) or await _followup(interaction, reply, ephemeral=fallback_ephemeral):
        return
    log.error("the reply of a successful interaction could not be delivered", extra=interaction_log_extra(interaction))


async def run_command(interaction: discord.Interaction[Any], work: Work, *, ephemeral: bool = False, overrides: Mapping[str, str] | None = None) -> None:
    """Run a slash command's work: defer with thinking (ephemeral when asked), send the returned Reply as the followup that replaces the thinking message, and on an exception from the work send the presenter's embed with the same visibility; when the defer fails the work does not run; a refused Reply is sent once more and then only logged; overrides replaces the messages of DomainError codes for this command."""
    if not await _defer(interaction, thinking=True, ephemeral=ephemeral):
        return
    try:
        reply = await work()
    except Exception as error:
        await send_error(interaction, error, ephemeral=ephemeral, overrides=overrides)
        return
    if reply is not None:
        await _deliver_followup(interaction, reply, ephemeral=ephemeral, fallback_ephemeral=ephemeral)


async def _replace_with_error(interaction: discord.Interaction[Any], error: BaseException, overrides: Mapping[str, str] | None) -> None:
    embed = present_error(error, overrides=overrides, log_extra=interaction_log_extra(interaction))
    try:
        await interaction.edit_original_response(content=None, embeds=[embed], view=None, attachments=[])
    except _SEND_ERRORS:
        log.warning("could not replace a pressed message with an error", exc_info=True, extra={"interaction_id": interaction.id})
        try:
            await interaction.followup.send(embed=embed, ephemeral=True)
        except _SEND_ERRORS:
            log.exception("could not send an error message for an interaction", extra={"interaction_id": interaction.id})


async def run_component(interaction: discord.Interaction[Any], work: Work, *, replace_message: bool = True, ephemeral: bool = True, overrides: Mapping[str, str] | None = None) -> None:
    """Run a button's work: with replace_message the press is deferred as a message update and the Reply, or on an exception from the work the presenter's embed, replaces the pressed message and removes its components (an ephemeral followup when that edit fails); otherwise it is deferred with thinking and the Reply or error is a new followup (ephemeral when asked), a refused Reply being sent once more as an ephemeral followup."""
    if replace_message:
        deferred = await _defer(interaction)
    else:
        deferred = await _defer(interaction, thinking=True, ephemeral=ephemeral)
    if not deferred:
        return
    try:
        reply = await work()
    except Exception as error:
        if replace_message:
            await _replace_with_error(interaction, error, overrides)
        else:
            await send_error(interaction, error, ephemeral=ephemeral, overrides=overrides)
        return
    if reply is None:
        return
    if not replace_message:
        await _deliver_followup(interaction, reply, ephemeral=ephemeral, fallback_ephemeral=True)
        return
    try:
        await interaction.edit_original_response(**reply.edit_kwargs())
    except _SEND_ERRORS:
        log.warning("could not replace a pressed message with its reply", exc_info=True, extra=interaction_log_extra(interaction))
    else:
        return
    if not await _followup(interaction, reply, ephemeral=True):
        log.error("the reply of a successful interaction could not be delivered", extra=interaction_log_extra(interaction))
