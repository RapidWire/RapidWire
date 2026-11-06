from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from rapidwire.core import allowances, claims, currency, discord_permissions, notifications, staking, tokens, transfers
from rapidwire.core.ledger import OperationResult
from rapidwire.core.types import ExecutionStatus, to_json_object

AT = datetime(2026, 5, 1, 12, 30, tzinfo=UTC)
BIG = 10**37 + 7

RESULTS: list[Any] = [
    transfers.TransferResult(execution_id=1, transfer_id=2, sender_id=3, recipient_id=4, currency_id=5, amount=BIG),
    transfers.TransferFromResult(execution_id=1, transfer_id=2, spender_id=3, owner_id=4, recipient_id=6, currency_id=5, amount=7, allowance_remaining=8),
    allowances.ApproveResult(execution_id=1, owner_id=2, spender_id=3, currency_id=4, old_amount=0, new_amount=BIG),
    claims.CreateClaimResult(execution_id=1, claim_id=2, claimant_id=3, payer_id=4, currency_id=5, amount=6, description=None),
    claims.CreateClaimResult(execution_id=1, claim_id=2, claimant_id=3, payer_id=4, currency_id=5, amount=6, description="lunch"),
    claims.PayClaimResult(execution_id=1, claim_id=2, transfer_id=3, payer_id=4, claimant_id=5, currency_id=6, amount=7),
    claims.CancelClaimResult(execution_id=1, claim_id=2, canceled_by=3),
    currency.CurrencyCreated(execution_id=1, currency_id=2, name="Coin", symbol="CN", hourly_rate_ppm=3, initial_supply=0, transfer_id=None, revived=False),
    currency.CurrencyCreated(execution_id=1, currency_id=2, name="Coin", symbol="CN", hourly_rate_ppm=3, initial_supply=BIG, transfer_id=9, revived=True),
    currency.SupplyChanged(execution_id=1, currency_id=2, account_id=3, amount=BIG, transfer_id=4),
    currency.MintingRenounced(execution_id=1, currency_id=2),
    currency.DelegationChanged(execution_id=1, currency_id=2, enabled=True, changed=False),
    currency.RateChangeRequested(execution_id=1, currency_id=2, new_rate_ppm=3, requested_at=AT, apply_after=AT + timedelta(days=7)),
    currency.RateChangeApplied(execution_id=1, currency_id=2, old_rate_ppm=3, new_rate_ppm=4, stakes_accrued=2, interest_execution_ids=(5, 6)),
    currency.DeleteRequested(execution_id=1, currency_id=2, requested_at=AT, finalize_from=AT + timedelta(days=7), finalize_until=AT + timedelta(days=10)),
    currency.CurrencyDeleted(execution_id=1, currency_id=2, burned_amount=BIG, balances_burned=3, stakes_burned=4, claims_canceled=5, allowances_deleted=6),
    staking.StakeChanged(execution_id=1, account_id=2, currency_id=3, amount=4, stake_amount=5, transfer_id=6, interest=0, interest_execution_id=None),
    staking.StakeChanged(execution_id=1, account_id=2, currency_id=3, amount=4, stake_amount=5, transfer_id=6, interest=7, interest_execution_id=8),
    notifications.NotificationChange(execution_id=1, user_id=2, allowed_user_id=3, changed=True),
    discord_permissions.DiscordPermissionChange(execution_id=1, guild_id=2, account_id=3, changed=False),
    tokens.TokenIssue(execution_id=1, token_id=2, revoked_token_ids=(3, 4)),
    tokens.TokenRevocation(execution_id=1, revoked_token_ids=()),
]


@pytest.mark.parametrize("result", RESULTS, ids=lambda result: type(result).__name__)
def test_stored_output_round_trips(result: Any) -> None:
    op = OperationResult(execution_id=result.execution_id, status=ExecutionStatus.SUCCESS, output=to_json_object(result.to_output()), replayed=True)
    restored = type(result).from_operation(op)
    assert restored == result
    assert restored.replayed


@pytest.mark.parametrize("result", RESULTS, ids=lambda result: type(result).__name__)
def test_replayed_takes_no_part_in_equality(result: Any) -> None:
    replayed = {item.name: item for item in dataclasses.fields(result)}["replayed"]
    assert not replayed.compare
    assert dataclasses.replace(result, replayed=True) == result
