"""The Python client against the real API app and test database: every endpoint group, idempotent replays, error classes, amounts, paging, contract values, backup downloads and the synchronous client."""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import assert_type

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.api.body_limit import DEFAULT_BODY_LIMIT
from rapidwire.config import Settings
from rapidwire.contract.compiler import compile_source
from rapidwire.core.permissions import InMemoryGuildInfo
from rapidwire_client import (
    Account,
    AsyncRapidWireClient,
    Attachment,
    AuthenticationError,
    Balance,
    ConflictError,
    Currency,
    NotFoundError,
    PermissionDeniedError,
    RapidWireClient,
    TransferResult,
    ValidationError,
    VersionInfo,
    from_units,
    new_idempotency_key,
)
from tests.integration.api.conftest import OPERATOR_ID, DiscordStub
from tests.integration.client.conftest import BASE_URL, ClientFactory, issue
from tests.integration.factories import balance_of, create_currency, mint

ALICE = 7301
BOB = 7302
CAROL = 7303
GUILD = 620_001
CONTRACT_OWNER = 7311
CALLER = 7312

SOURCE = """from rapidwire_contract import *

@public
def echo(a: int, b: str, c: bool, d: Account, e: Currency) -> str:
    return str(a) + b + str(int(c)) + str(int(d)) + str(int(e))

@public
def fill(who: Account) -> int:
    storage["list"] = [1, -2, 3]
    m: dict[str, Account] = {"bb": who, "a": Account(5)}
    storage["dict"] = m
    storage["a/b"] = "slash"
    return 3

@public
def buy(price: int) -> int:
    if paid.amount < price:
        revert("Send at least the price")
    return paid.amount

@public
def fail() -> int:
    storage["x"] = 1
    revert("nope")
"""


async def test_meta(rapidwire: ClientFactory) -> None:
    client = await rapidwire(None)
    version = await client.get_version()
    assert_type(version, VersionInfo)
    assert version.name == "RapidWire API"
    config = await client.get_config()
    assert config.amounts.minimal_unit == Decimal("0.001")
    assert config.amounts.max_amount == from_units(10**38 - 1)
    assert config.staking.rate_percent_per_hour_max == Decimal("100.0000")
    assert config.paging.limits["balances"].max == 100
    assert config.paging.max_window == 10_000
    assert 100 in config.swap.fee_tiers_bps
    assert isinstance(config.gas.currency_id, int)
    assert config.max_body_bytes == DEFAULT_BODY_LIMIT


async def test_currency_management_transfers_and_replay(rapidwire: ClientFactory, guild_info: InMemoryGuildInfo) -> None:
    guild_info.add_guild(GUILD, ALICE)
    alice = await rapidwire(ALICE)
    created = await alice.create_currency(GUILD, "Gold", "GLD", Decimal("1000.5"), hourly_rate_ppm=10)
    assert (created.currency_id, created.symbol, created.initial_supply, created.hourly_rate_ppm, created.revived) == (GUILD, "GLD", Decimal("1000.500"), 10, False)
    assert isinstance(created.transfer_id, int)

    key = new_idempotency_key()
    sent = await alice.transfer(BOB, GUILD, "12.345", idempotency_key=key)
    assert_type(sent, TransferResult)
    again = await alice.transfer(BOB, GUILD, "12.345", idempotency_key=key)
    assert (sent.replayed, again.replayed) == (False, True)
    assert (again.execution_id, again.transfer_id, again.amount) == (sent.execution_id, sent.transfer_id, Decimal("12.345"))
    with pytest.raises(ValidationError) as conflict:
        await alice.transfer(BOB, GUILD, "1", idempotency_key=key)
    assert conflict.value.code == "idempotency_conflict"

    minted = await alice.mint(GUILD, from_units(2500))
    burned = await alice.burn(GUILD, "0.5")
    assert (minted.amount, burned.amount, minted.account_id) == (Decimal("2.500"), Decimal("0.500"), ALICE)
    assert (await alice.enable_delegation(GUILD)).enabled is True
    assert (await alice.disable_delegation(GUILD)).changed is True
    requested = await alice.request_rate_change(GUILD, 20)
    assert requested.new_rate_ppm == 20
    assert requested.apply_after > requested.requested_at
    with pytest.raises(ConflictError) as early:
        await alice.apply_rate_change(GUILD)
    assert early.value.code == "timelock_active"
    assert early.value.execution_id is not None

    public = await rapidwire(None)
    balance = await public.get_balance(ALICE, GUILD)
    assert balance.amount == Decimal("1000.500") - Decimal("12.345") + Decimal("2.000")
    assert [item.currency_id for item in (await public.list_balances(ALICE)).items] == [GUILD]
    info = await public.get_currency_by_symbol("gld")
    assert (info.currency_id, info.pending_rate_ppm, info.supply) == (GUILD, 20, Decimal("1002.500"))
    issuance_id = (await public.get_currency(GUILD)).issuance_account_id
    assert issuance_id < 0
    issuance = await public.get_account(issuance_id)
    assert (issuance.kind, issuance.currency_id, issuance.pool_id) == ("issuance", GUILD, None)
    assert (await public.get_account(BOB)).kind == "user"
    with pytest.raises(NotFoundError) as no_account:
        await public.get_account(CAROL)
    assert no_account.value.code == "account_not_found"
    assert [item.symbol for item in await public.list_currencies()] == ["GLD"]
    entry = await public.get_transfer(sent.transfer_id)
    assert (entry.from_account, entry.to_account, entry.amount, entry.execution_type) == (ALICE, BOB, Decimal("12.345"), "transfer")
    detail = await public.get_execution(sent.execution_id)
    assert detail.has_idempotency_key is True
    assert [transfer.transfer_id for transfer in detail.transfers] == [sent.transfer_id]
    assert detail.input["amount"] == "12345"
    executions = await public.list_executions(initiator_id=ALICE, execution_type="transfer")
    assert [item.execution_id for item in executions.items] == [sent.execution_id]
    assert (executions.items[0].contract_id, executions.items[0].parent_execution_id) == (None, None)
    assert (await public.list_executions(contract_id=ALICE)).items == ()
    found = await public.search_history(source_id=ALICE, destination_id=BOB, min_amount="12.345", max_amount=Decimal(13), start=datetime.now(UTC) - timedelta(hours=1))
    assert [item.transfer_id for item in found.items] == [sent.transfer_id]
    history = await public.get_account_history(BOB, currency_id=GUILD, sort_by="amount", sort_order="asc")
    assert [item.transfer_id for item in history.items] == [sent.transfer_id]
    stats = await public.get_user_stats(BOB)
    assert stats.total_transfers == 1

    assert (await alice.renounce_minting(GUILD)).currency_id == GUILD
    with pytest.raises(ConflictError) as renounced:
        await alice.mint(GUILD, "1")
    assert renounced.value.code == "currency_renounced"
    deletion = await alice.request_currency_deletion(GUILD)
    assert deletion.finalize_until > deletion.finalize_from
    with pytest.raises(ConflictError) as finalize:
        await alice.finalize_currency_deletion(GUILD)
    assert finalize.value.code == "timelock_active"


async def test_claims_allowances_and_staking(rapidwire: ClientFactory, db: AsyncEngine) -> None:
    currency_id = 5301
    await create_currency(db, currency_id=currency_id, symbol="CLM", hourly_rate_ppm=100)
    await mint(db, currency_id, ALICE, 100_000)
    alice, bob = await rapidwire(ALICE), await rapidwire(BOB)

    approved = await alice.approve(BOB, currency_id, "10")
    assert (approved.old_amount, approved.new_amount) == (Decimal("0.000"), Decimal("10.000"))
    moved = await bob.transfer_from(ALICE, CAROL, currency_id, "4")
    assert (moved.spender_id, moved.source_id, moved.allowance_remaining) == (BOB, ALICE, Decimal("6.000"))
    assert (await bob.get_allowance(ALICE, BOB, currency_id)).amount == Decimal("6.000")
    changes = await bob.list_allowance_history(ALICE, spender_id=BOB)
    assert [change.new_amount for change in changes.items] == [Decimal("6.000"), Decimal("10.000")]

    claim = await bob.create_claim(ALICE, currency_id, "3", description="lunch")
    other = await bob.create_claim(ALICE, currency_id, "1")
    assert (claim.description, other.description) == ("lunch", None)
    pending = await alice.list_claims(ALICE, status="pending")
    assert {item.claim_id for item in pending.items} == {claim.claim_id, other.claim_id}
    paid = await alice.pay_claim(claim.claim_id)
    assert (paid.payer_id, paid.claimant_id, paid.amount) == (ALICE, BOB, Decimal("3.000"))
    canceled = await alice.cancel_claim(other.claim_id)
    assert canceled.canceled_by == ALICE
    resolved = await alice.get_claim(claim.claim_id)
    assert (resolved.created_execution_id, resolved.resolved_execution_id) == (claim.execution_id, paid.execution_id)
    assert [item.claim_id for item in (await alice.list_claims(ALICE, role="claimant")).items] == []
    assert {item.claim_id for item in (await alice.list_claims(ALICE, role="payer")).items} == {claim.claim_id, other.claim_id}
    with pytest.raises(ConflictError) as again:
        await alice.pay_claim(claim.claim_id)
    assert again.value.code == "invalid_state"

    deposited = await alice.deposit_stake(currency_id, "50")
    withdrawn = await alice.withdraw_stake(currency_id, "20")
    assert (deposited.stake_amount, withdrawn.amount) == (Decimal("50.000"), Decimal("20.000"))
    stake = await alice.get_stake(ALICE, currency_id)
    assert stake.hourly_rate_ppm == 100
    assert stake.projected_amount >= stake.amount >= Decimal("30.000")
    assert [item.currency_id for item in (await alice.list_stakes(ALICE)).items] == [currency_id]
    with pytest.raises(NotFoundError) as missing:
        await alice.get_stake(BOB, currency_id)
    assert missing.value.code == "stake_not_found"


async def test_tokens_notifications_and_discord_permissions(rapidwire: ClientFactory, guild_info: InMemoryGuildInfo, discord: DiscordStub) -> None:
    guild_info.add_guild(GUILD, ALICE)
    alice = await rapidwire(ALICE)
    allowed = await alice.allow_notifications(BOB)
    assert (allowed.account_id, allowed.allowed_user_id, allowed.changed) == (ALICE, BOB, True)
    assert [item.allowed_user_id for item in (await alice.list_notifications()).items] == [BOB]
    assert (await alice.deny_notifications(BOB)).changed is True

    granted = await alice.allow_discord(GUILD, CAROL)
    assert (granted.guild_id, granted.account_id, granted.changed) == (GUILD, CAROL, True)
    listed = await alice.list_discord_permissions(GUILD)
    assert [(item.account_id, item.granted_by) for item in listed.items] == [(CAROL, ALICE)]
    assert (await alice.deny_discord(GUILD, CAROL)).changed is True
    with pytest.raises(PermissionDeniedError):
        await (await rapidwire(BOB)).list_discord_permissions(GUILD)

    discord.users[ALICE] = "alice_name"
    assert (await alice.get_username(ALICE)).username == "alice_name"
    with pytest.raises(NotFoundError) as unknown:
        await alice.get_username(BOB)
    assert unknown.value.code == "user_not_found"

    tokens = await alice.list_tokens()
    assert [token.active for token in tokens.items] == [True]
    revoked = await alice.revoke_tokens()
    assert revoked.revoked_token_ids == (tokens.items[0].token_id,)
    with pytest.raises(AuthenticationError) as rejected:
        await alice.list_tokens(include_revoked=False)
    assert rejected.value.code == "unauthorized"


async def test_error_classes_and_recorded_failures(rapidwire: ClientFactory, db: AsyncEngine) -> None:
    currency_id = 5302
    await create_currency(db, currency_id=currency_id, symbol="ERR")
    anonymous, carol = await rapidwire(None), await rapidwire(CAROL)
    with pytest.raises(AuthenticationError):
        await anonymous.transfer(BOB, currency_id, "1")
    with pytest.raises(NotFoundError) as missing:
        await anonymous.get_currency(999_999)
    assert missing.value.code == "currency_not_found"
    with pytest.raises(PermissionDeniedError):
        await carol.pause_contract(ALICE)

    key = new_idempotency_key()
    with pytest.raises(ConflictError) as first:
        await carol.transfer(ALICE, currency_id, "1", idempotency_key=key)
    with pytest.raises(ConflictError) as replay:
        await carol.transfer(ALICE, currency_id, "1", idempotency_key=key)
    assert first.value.code == replay.value.code == "insufficient_balance"
    assert first.value.execution_id is not None
    assert replay.value.execution_id == first.value.execution_id
    assert (first.value.replayed, replay.value.replayed) == (False, True)
    failed = await anonymous.get_execution(first.value.execution_id)
    assert failed.status == "failed"

    with pytest.raises(ValidationError) as invalid:
        await carol.transfer(ALICE, currency_id, "0")
    assert invalid.value.code == "validation_error"
    assert invalid.value.details is not None
    assert invalid.value.details[0].loc == ("body", "amount")


async def test_amounts_round_trip(rapidwire: ClientFactory, db: AsyncEngine) -> None:
    currency_id = 5303
    await create_currency(db, currency_id=currency_id, symbol="AMT")
    await mint(db, currency_id, ALICE, 10**21 + 7)
    alice = await rapidwire(ALICE)
    for amount in (from_units(1), Decimal("12.345"), "7.5", Decimal("1E+3")):
        result = await alice.transfer(BOB, currency_id, amount)
        assert result.amount == Decimal(amount)
        assert len(str(result.amount).split(".")[1]) == 3
    expected = from_units(10**21 + 7) - Decimal("0.001") - Decimal("12.345") - Decimal("7.5") - Decimal(1000)
    balance = await alice.get_balance(ALICE, currency_id)
    assert balance.amount == expected
    assert await balance_of(db, ALICE, currency_id) == 10**21 + 7 - 1 - 12_345 - 7_500 - 1_000_000
    supply = (await alice.get_currency(currency_id)).supply
    assert supply == from_units(10**21 + 7)
    with pytest.raises(TypeError):
        await alice.transfer(BOB, currency_id, 1.5)  # type: ignore[arg-type]


async def test_pagination_iterator(rapidwire: ClientFactory, db: AsyncEngine) -> None:
    ids = [5400 + index for index in range(7)]
    for index, currency_id in enumerate(ids):
        await create_currency(db, currency_id=currency_id, symbol=f"PG{chr(65 + index)}")
        await mint(db, currency_id, ALICE, 1000 * (index + 1))
    client = await rapidwire(None)
    first = await client.list_balances(ALICE, per_page=3)
    assert (first.page, first.per_page, first.total, first.total_is_lower_bound, first.has_next) == (1, 3, 7, False, True)
    walked = [balance async for balance in client.paginate(client.list_balances, ALICE, per_page=3)]
    assert all(isinstance(balance, Balance) for balance in walked)
    assert [balance.currency_id for balance in walked] == ids
    assert [balance.amount for balance in walked] == [Decimal(index + 1) for index in range(7)]


async def test_dex(rapidwire: ClientFactory, db: AsyncEngine) -> None:
    x, y = 5501, 5502
    for currency_id, symbol in ((x, "DXA"), (y, "DXB")):
        await create_currency(db, currency_id=currency_id, symbol=symbol)
        await mint(db, currency_id, ALICE, 10_000_000)
    alice = await rapidwire(ALICE)
    created = await alice.create_pool(y, x, 100, "1000", "1000", idempotency_key="create-pool-1")
    assert (created.currency_a, created.currency_b, created.fee_bps, created.reserve_a) == (x, y, 100, Decimal("1000.000"))
    assert created.custody_account_id < 0
    assert (await alice.create_pool(y, x, 100, "1000", "1000", idempotency_key="create-pool-1")).replayed is True
    public = await rapidwire(None)
    pools = await public.list_pools()
    assert [pool.pool_id for pool in pools.items] == [created.pool_id]
    assert (await public.get_pool(created.pool_id)).total_shares == created.total_shares
    assert [pool.fee_bps for pool in (await public.get_pair_pools(y, x)).pools] == [100]

    quote = await public.quote_swap(x, y, "10")
    assert quote.amount_out > 0
    assert quote.symbols == {x: "DXA", y: "DXB"}
    assert sum(route.amount_in for route in quote.routes) == Decimal("10.000")
    min_out = (quote.amount_out * Decimal("0.99")).quantize(Decimal("0.001"), rounding=ROUND_DOWN)
    swapped = await alice.swap(x, y, "10", min_out)
    assert swapped.amount_out >= min_out
    assert swapped.transfer_ids
    with pytest.raises(ConflictError) as slipped:
        await alice.swap(x, y, "10", quote.amount_out * 2)
    assert slipped.value.code == "slippage_exceeded"

    added = await alice.add_liquidity(x, y, 100, "10", "10", amount_x_min="0", min_shares=1)
    assert added.shares > 0
    removed = await alice.remove_liquidity(x, y, 100, added.shares, amount_y_min=Decimal(0))
    assert removed.shares == added.shares
    [position] = (await public.list_positions(ALICE)).items
    assert (position.pool.pool_id, position.shares) == (created.pool_id, removed.provider_shares)
    assert Decimal(0) < position.share_percent <= Decimal(100)


async def test_contracts(rapidwire: ClientFactory, db: AsyncEngine, api_settings: Settings) -> None:
    gas = api_settings.gas_currency_id
    await create_currency(db, currency_id=gas, symbol="RAPID", name="Rapid")
    await mint(db, gas, CALLER, 1_000_000)
    binary = compile_source(SOURCE)
    owner, caller, anonymous = await rapidwire(CONTRACT_OWNER), await rapidwire(CALLER), await rapidwire(None)

    registered = await owner.register_contract(CONTRACT_OWNER, binary)
    assert (registered.version, registered.sha256, registered.status) == (1, hashlib.sha256(binary).hexdigest(), "active")
    info = await anonymous.get_contract(CONTRACT_OWNER)
    assert [fn.name for fn in info.functions] == ["buy", "echo", "fail", "fill"]
    echo = next(fn for fn in (await anonymous.get_contract_functions(CONTRACT_OWNER)).functions if fn.name == "echo")
    assert echo.params == ("int", "str", "bool", "Account", "Currency")
    assert [item.version for item in (await anonymous.list_contract_versions(CONTRACT_OWNER)).items] == [1]
    assert (await anonymous.get_contract_version(CONTRACT_OWNER, 1)).binary == binary
    downloaded = await anonymous.download_contract_binary(CONTRACT_OWNER, 1)
    assert (downloaded.data, downloaded.sha256, downloaded.filename) == (binary, registered.sha256, f"contract-{CONTRACT_OWNER}-v1.rwc")

    called = await caller.call_contract(CONTRACT_OWNER, "echo", [12, "x", True, Account(34), Currency(56)], idempotency_key="call-echo")
    assert (called.value, called.contract_id, called.gas_currency_id, called.gas_price, called.random) == ("12x13456", CONTRACT_OWNER, gas, api_settings.gas_price, ())
    assert called.gas_cost == from_units(called.gas_used)
    assert (await caller.call_contract(CONTRACT_OWNER, "echo", [12, "x", True, Account(34), Currency(56)], idempotency_key="call-echo")).replayed is True
    stored = await caller.call_contract(CONTRACT_OWNER, "fill", [Account(CALLER)])
    assert stored.value == 3
    entries = {entry.key: entry.value for entry in (await anonymous.list_storage(CONTRACT_OWNER)).items}
    assert entries == {"a/b": "slash", "dict": {"a": Account(5), "bb": Account(CALLER)}, "list": [1, -2, 3]}
    stored_dict = entries["dict"]
    assert isinstance(stored_dict, dict)
    assert list(stored_dict) == ["a", "bb"]
    slashed = await anonymous.get_storage(CONTRACT_OWNER, "a/b")
    assert (slashed.key, slashed.value) == ("a/b", "slash")
    assert slashed.updated_at.tzinfo is not None
    assert slashed.updated_at == next(entry.updated_at for entry in (await anonymous.list_storage(CONTRACT_OWNER)).items if entry.key == "a/b")
    with pytest.raises(NotFoundError):
        await anonymous.get_storage(CONTRACT_OWNER, "absent")

    paid = await caller.call_contract(CONTRACT_OWNER, "buy", [400], attach=Attachment(currency_id=gas, amount="0.5"), gas_limit=1000)
    assert (paid.value, paid.gas_limit) == (500, 1000)
    with pytest.raises(ValidationError) as reverted:
        await caller.call_contract(CONTRACT_OWNER, "fail")
    assert (reverted.value.code, reverted.value.kind, reverted.value.message) == ("contract_reverted", "REVERTED", "nope")
    assert reverted.value.execution_id is not None
    with pytest.raises(ValidationError) as unknown:
        await caller.call_contract(CONTRACT_OWNER, "nothing")
    assert (unknown.value.kind, unknown.value.reason) == ("CALL_FAILED", "NO_FUNCTION")

    with pytest.raises(AuthenticationError):
        await anonymous.dry_run_contract(CONTRACT_OWNER, "buy", [100], attach=Attachment(currency_id=gas, amount="0.2"))
    dry = await caller.dry_run_contract(CONTRACT_OWNER, "buy", [100], attach=Attachment(currency_id=gas, amount="0.2"))
    assert (dry.ok, dry.value, dry.error, dry.gas_currency_id, dry.gas_price) == (True, 200, None, gas, api_settings.gas_price)
    assert dry.gas_cost == from_units(dry.gas_used * api_settings.gas_price)
    assert sorted(item.kind for item in dry.transfers) == ["gas", "principal"]
    assert {item.from_account for item in dry.transfers} == {CALLER}
    failed_dry = await caller.dry_run_contract(CONTRACT_OWNER, "fail")
    assert failed_dry.ok is False
    assert failed_dry.error is not None
    assert failed_dry.error.code == "contract_reverted"

    assert (await owner.pause_contract(CONTRACT_OWNER)).status == "paused"
    with pytest.raises(ConflictError) as paused:
        await caller.call_contract(CONTRACT_OWNER, "echo", [1, "", False, Account(1), Currency(1)])
    assert (paused.value.code, paused.value.reason) == ("contract_paused", "INACTIVE")
    assert (await owner.resume_contract(CONTRACT_OWNER)).status == "active"
    until = datetime.now(UTC) + timedelta(days=1)
    locked = await owner.lock_contract(CONTRACT_OWNER, until=until)
    assert abs(locked.locked_until - until) < timedelta(seconds=1)
    lock_key = new_idempotency_key()
    extended = await owner.lock_contract(CONTRACT_OWNER, hours=48, idempotency_key=lock_key)
    assert extended.previous_locked_until == locked.locked_until
    replayed = await owner.lock_contract(CONTRACT_OWNER, hours=48, idempotency_key=lock_key)
    assert (replayed.replayed, replayed.locked_until, replayed.execution_id) == (True, extended.locked_until, extended.execution_id)
    with pytest.raises(ConflictError) as locked_error:
        await owner.delete_contract(CONTRACT_OWNER)
    assert locked_error.value.code == "contract_locked"

    second = await rapidwire(CALLER)
    await second.register_contract(CALLER, binary)
    deleted = await second.delete_contract(CALLER)
    assert (deleted.version, deleted.storage_keys_cleared) == (1, 0)


def _write_backup(directory: Path, name: str, data: bytes) -> None:
    (directory / name).write_bytes(data)


async def test_backups(rapidwire: ClientFactory, backup_dir: Path, tmp_path: Path) -> None:
    data = os.urandom(200_000)
    digest = hashlib.sha256(data).hexdigest()
    _write_backup(backup_dir, "rapidwire-dump-20261031T030000Z.sql.gz", data)
    _write_backup(backup_dir, "small.jsonl", b"{}\n")
    operator = await rapidwire(OPERATOR_ID)
    listing = await operator.list_backups()
    assert {item.name: item.sha256 for item in listing.items}["rapidwire-dump-20261031T030000Z.sql.gz"] == digest
    assert listing.execution_id > 0
    assert (listing.page, listing.per_page, listing.total, listing.has_next) == (1, 15, 2, False)
    first = await operator.list_backups(per_page=1)
    assert (len(first.items), first.total, first.has_next) == (1, 2, True)
    assert sorted([item.name async for item in operator.paginate(operator.list_backups, per_page=1)]) == ["rapidwire-dump-20261031T030000Z.sql.gz", "small.jsonl"]

    target = tmp_path / "out" / "dump.sql.gz"
    target.parent.mkdir()
    result = await operator.download_backup("rapidwire-dump-20261031T030000Z.sql.gz", target)
    assert target.read_bytes() == data
    assert (result.size, result.sha256, result.path, result.content_type) == (len(data), digest, target, "application/gzip")
    assert result.execution_id is not None
    assert sorted(path.name for path in target.parent.iterdir()) == ["dump.sql.gz"]
    assert (await operator.get_execution(result.execution_id)).type == "backup_download"

    async with operator.stream_backup("small.jsonl") as stream:
        assert (stream.sha256, stream.size) == (hashlib.sha256(b"{}\n").hexdigest(), 3)
        chunks = [chunk async for chunk in stream]
    assert b"".join(chunks) == b"{}\n"
    assert stream.result.size == 3
    sink = io.BytesIO()
    assert (await operator.download_backup("small.jsonl", sink)).path is None
    assert sink.getvalue() == b"{}\n"

    with pytest.raises(NotFoundError):
        await operator.download_backup("missing.gz", tmp_path / "missing.gz")
    assert not (tmp_path / "missing.gz").exists()
    with pytest.raises(PermissionDeniedError):
        await (await rapidwire(ALICE)).list_backups()
    with pytest.raises(PermissionDeniedError):
        await (await rapidwire(ALICE)).download_backup("small.jsonl", io.BytesIO())
    # The client sends a slash in a name encoded, and the server refuses such a name after the operator check.
    with pytest.raises(PermissionDeniedError):
        await (await rapidwire(ALICE)).download_backup("a/small.jsonl", io.BytesIO())
    with pytest.raises(NotFoundError):
        await operator.download_backup("a/small.jsonl", io.BytesIO())


class _LoopTransport(httpx.BaseTransport):
    """A synchronous transport for a client running in a worker thread: each request is served by the ASGI app on the test's event loop."""

    def __init__(self, app: FastAPI, loop: asyncio.AbstractEventLoop) -> None:
        self._asgi = httpx.ASGITransport(app=app)
        self._loop = loop

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        response = await self._asgi.handle_async_request(request)
        content = await response.aread()
        return httpx.Response(response.status_code, headers=response.headers, content=content)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return asyncio.run_coroutine_threadsafe(self._handle(request), self._loop).result(timeout=60)


async def test_sync_client(api_app: FastAPI, db: AsyncEngine, backup_dir: Path, tmp_path: Path) -> None:
    currency_id = 5601
    await create_currency(db, currency_id=currency_id, symbol="SYN")
    await mint(db, currency_id, ALICE, 50_000)
    for index in range(3):
        await create_currency(db, currency_id=5602 + index, symbol=f"SY{chr(65 + index)}")
        await mint(db, 5602 + index, ALICE, 1)
    _write_backup(backup_dir, "sync.sql.gz", b"sync backup")
    alice_token, operator_token = await issue(db, ALICE), await issue(db, OPERATOR_ID)
    transport = _LoopTransport(api_app, asyncio.get_running_loop())

    def work() -> None:
        with RapidWireClient(BASE_URL, alice_token, transport=transport) as alice:
            assert_type(alice.get_version(), VersionInfo)
            key = new_idempotency_key()
            sent = alice.transfer(BOB, currency_id, Decimal("1.25"), idempotency_key=key)
            assert_type(sent, TransferResult)
            again = alice.transfer(BOB, currency_id, Decimal("1.25"), idempotency_key=key)
            assert (sent.replayed, again.replayed, again.execution_id) == (False, True, sent.execution_id)
            assert alice.get_balance(BOB, currency_id).amount == Decimal("1.250")
            assert [balance.currency_id for balance in alice.paginate(alice.list_balances, ALICE, per_page=2)] == [5601, 5602, 5603, 5604]
            with pytest.raises(ConflictError) as error:
                alice.transfer(BOB, currency_id, "1000")
            assert error.value.code == "insufficient_balance"
            with pytest.raises(PermissionDeniedError):
                alice.list_backups()
        with RapidWireClient(BASE_URL, operator_token, transport=transport) as operator:
            result = operator.download_backup("sync.sql.gz", tmp_path / "sync.sql.gz")
            assert result.sha256 == hashlib.sha256(b"sync backup").hexdigest()
            with operator.stream_backup("sync.sql.gz") as stream:
                assert b"".join(stream) == b"sync backup"
        with RapidWireClient(BASE_URL, transport=transport) as anonymous, pytest.raises(AuthenticationError):
            anonymous.transfer(BOB, currency_id, "1")

    await asyncio.to_thread(work)
    assert (tmp_path / "sync.sql.gz").read_bytes() == b"sync backup"


async def test_async_client_closes_its_connections(api_app: FastAPI) -> None:
    async with AsyncRapidWireClient(BASE_URL, "secret-token-value", transport=httpx.ASGITransport(app=api_app)) as client:
        assert "secret-token-value" not in repr(client)
        await client.get_version()
    with pytest.raises(RuntimeError):
        await client.get_version()
