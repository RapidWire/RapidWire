"""The slash command tree pinned to the feature inventory: every command, group and subcommand with each parameter's name, Discord type, required flag, fixed choices and autocomplete."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator
from typing import Any, NamedTuple

import discord
import pytest
from discord import AppCommandOptionType, app_commands

from rapidwire.bot.app import build_bot
from rapidwire.config import Settings

STRING = AppCommandOptionType.string
INTEGER = AppCommandOptionType.integer
USER = AppCommandOptionType.user
ATTACHMENT = AppCommandOptionType.attachment

# Discord's limits on a command: options, subcommands per group, name and description lengths.
MAX_OPTIONS = 25
NAME_RE = re.compile(r"[-_a-z0-9]{1,32}")
DESCRIPTION_MAX = 100


class Param(NamedTuple):
    name: str
    type: AppCommandOptionType
    required: bool
    autocomplete: bool = False
    choices: tuple[tuple[str, int | float | str], ...] = ()


def req(name: str, kind: AppCommandOptionType, *, autocomplete: bool = False, choices: tuple[tuple[str, int | float | str], ...] = ()) -> Param:
    return Param(name, kind, True, autocomplete, choices)


def opt(name: str, kind: AppCommandOptionType, *, autocomplete: bool = False, choices: tuple[tuple[str, int | float | str], ...] = ()) -> Param:
    return Param(name, kind, False, autocomplete, choices)


FEE_TIERS = (("1%", 100), ("0.5%", 50), ("0.1%", 10))
CLAIM_ROLES = (("請求者として", "claimant"), ("支払者として", "payer"))

# Every invocable command by qualified name, with its parameters in order.
EXPECTED: dict[str, tuple[Param, ...]] = {
    "balance": (opt("user", USER), opt("page", INTEGER)),
    "transfer": (req("user", USER), req("amount", STRING), opt("symbol", STRING, autocomplete=True)),
    "transfer_from": (req("source", USER), req("destination", USER), req("amount", STRING), opt("symbol", STRING, autocomplete=True)),
    "history": (
        opt("transfer_id", INTEGER),
        opt("user", USER),
        opt("source", USER),
        opt("destination", USER),
        opt("currency_symbol", STRING, autocomplete=True),
        opt("start_date", STRING),
        opt("end_date", STRING),
        opt("min_amount", STRING),
        opt("max_amount", STRING),
        opt("execution_type", STRING, autocomplete=True),
        opt("contract_function", STRING),
        opt("page", INTEGER),
    ),
    "currency create": (req("name", STRING), req("symbol", STRING), req("supply", STRING), opt("hourly_interest_rate", STRING)),
    "currency info": (opt("symbol", STRING, autocomplete=True),),
    "currency mint": (req("amount", STRING),),
    "currency burn": (req("amount", STRING),),
    "currency renounce": (),
    "currency delete": (),
    "currency request-interest-change": (req("rate", STRING),),
    "currency apply-interest-change": (),
    "currency delegation enable": (),
    "currency delegation disable": (),
    "stake deposit": (req("amount", STRING), opt("symbol", STRING, autocomplete=True)),
    "stake withdraw": (req("amount", STRING), opt("symbol", STRING, autocomplete=True)),
    "stake info": (opt("page", INTEGER),),
    "approve set": (req("user", USER), req("amount", STRING), opt("symbol", STRING, autocomplete=True)),
    "approve info": (req("user", USER), opt("symbol", STRING, autocomplete=True)),
    "contract set": (req("binary", ATTACHMENT), opt("lock_hours", INTEGER)),
    "contract get": (opt("user", USER),),
    "contract call": (
        req("user", USER),
        req("function", STRING, autocomplete=True),
        opt("args", STRING, autocomplete=True),
        opt("pay", STRING),
        opt("currency", STRING, autocomplete=True),
        opt("gas", INTEGER),
    ),
    "contract info": (opt("user", USER),),
    "contract lock": (req("hours", INTEGER),),
    "contract pause": (),
    "contract resume": (),
    "contract delete": (),
    "claim create": (req("user", USER), req("amount", STRING), opt("description", STRING)),
    "claim list": (opt("page", INTEGER), opt("role", STRING, choices=CLAIM_ROLES)),
    "claim pay": (req("claim_id", INTEGER),),
    "claim cancel": (req("claim_id", INTEGER),),
    "lp create": (
        req("symbol_a", STRING, autocomplete=True),
        req("amount_a", STRING),
        req("symbol_b", STRING, autocomplete=True),
        req("amount_b", STRING),
        req("fee_tier", INTEGER, choices=FEE_TIERS),
    ),
    "lp add": (
        req("symbol_a", STRING, autocomplete=True),
        req("amount_a", STRING),
        req("symbol_b", STRING, autocomplete=True),
        req("amount_b", STRING),
        req("fee_tier", INTEGER, choices=FEE_TIERS),
    ),
    "lp remove": (
        req("symbol_a", STRING, autocomplete=True),
        req("symbol_b", STRING, autocomplete=True),
        req("shares", STRING),
        req("fee_tier", INTEGER, choices=FEE_TIERS),
    ),
    "lp info": (req("symbol_a", STRING, autocomplete=True), req("symbol_b", STRING, autocomplete=True)),
    "lp list": (opt("user", USER), opt("page", INTEGER)),
    "swap": (req("from_symbol", STRING, autocomplete=True), req("to_symbol", STRING, autocomplete=True), req("amount", STRING)),
    "notification allow": (req("user", USER),),
    "notification deny": (req("user", USER),),
    # The two permission lists are paged like /claim list.
    "notification list": (opt("page", INTEGER),),
    "discord-permission allow": (req("user", USER),),
    "discord-permission deny": (req("user", USER),),
    "discord-permission list": (opt("page", INTEGER),),
}

# Symbol parameters that name a new currency rather than an existing one, so there is nothing to complete.
NEW_SYMBOL_PARAMS = frozenset({("currency create", "symbol")})

type AppCommand = app_commands.Command[Any, ..., Any]


def walk(tree: app_commands.CommandTree[Any]) -> Iterator[AppCommand | app_commands.Group]:
    """Every command and group of the tree, groups before their children."""
    pending: list[Any] = list(tree.get_commands())
    while pending:
        item = pending.pop(0)
        assert isinstance(item, app_commands.Command | app_commands.Group), item
        yield item
        if isinstance(item, app_commands.Group):
            pending.extend(item.commands)


def invocable(tree: app_commands.CommandTree[Any]) -> dict[str, AppCommand]:
    """The invocable commands of the tree by qualified name."""
    return {item.qualified_name: item for item in walk(tree) if isinstance(item, app_commands.Command)}


def actual_params(command: AppCommand) -> tuple[Param, ...]:
    return tuple(Param(param.name, param.type, param.required, param.autocomplete, tuple((choice.name, choice.value) for choice in param.choices)) for param in command.parameters)


@pytest.fixture(scope="module")
def tree() -> Iterator[app_commands.CommandTree[Any]]:
    bot = build_bot(Settings(_env_file=None, db_password="unit-test-password"))
    yield bot.tree
    asyncio.run(bot.context.engine.dispose())


def test_the_tree_has_exactly_the_inventory_commands(tree: app_commands.CommandTree[Any]) -> None:
    assert sorted(invocable(tree)) == sorted(EXPECTED)


def test_the_groups_are_the_prefixes_of_the_commands(tree: app_commands.CommandTree[Any]) -> None:
    groups = {item.qualified_name for item in walk(tree) if isinstance(item, app_commands.Group)}
    expected = {" ".join(name.split()[:depth]) for name in EXPECTED for depth in range(1, len(name.split()))}
    assert groups == expected
    assert expected == {"currency", "currency delegation", "stake", "approve", "contract", "claim", "lp", "notification", "discord-permission"}


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_command_parameters_match_the_inventory(tree: app_commands.CommandTree[Any], name: str) -> None:
    command = invocable(tree).get(name)
    assert command is not None, name
    assert actual_params(command) == EXPECTED[name]


def test_every_existing_symbol_and_currency_parameter_has_autocomplete(tree: app_commands.CommandTree[Any]) -> None:
    missing = [
        (name, param.name)
        for name, command in invocable(tree).items()
        for param in command.parameters
        if ("symbol" in param.name or param.name == "currency") and (name, param.name) not in NEW_SYMBOL_PARAMS and not param.autocomplete
    ]
    assert missing == []


def test_discord_limits_hold(tree: app_commands.CommandTree[Any]) -> None:
    for item in walk(tree):
        assert NAME_RE.fullmatch(item.name), item.qualified_name
        assert 1 <= len(item.description) <= DESCRIPTION_MAX, item.qualified_name
        if isinstance(item, app_commands.Group):
            assert 1 <= len(item.commands) <= MAX_OPTIONS, item.qualified_name
            continue
        assert len(item.parameters) <= MAX_OPTIONS, item.qualified_name
        for param in item.parameters:
            assert NAME_RE.fullmatch(param.name), (item.qualified_name, param.name)
            assert 1 <= len(param.description) <= DESCRIPTION_MAX, (item.qualified_name, param.name)
            assert len(param.choices) <= MAX_OPTIONS, (item.qualified_name, param.name)


def test_only_discord_permission_is_restricted_to_administrators_in_guilds(tree: app_commands.CommandTree[Any]) -> None:
    restricted = tree.get_command("discord-permission")
    assert isinstance(restricted, app_commands.Group)
    assert restricted.default_permissions == discord.Permissions(administrator=True)
    assert restricted.guild_only
    others = [item.name for item in tree.get_commands() if item.name != "discord-permission" and getattr(item, "default_permissions", None) is not None]
    assert others == []
