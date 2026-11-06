"""Dice lottery: bet RAPID on a number from 1 to SIDES and win PAYOUT_MULTIPLIER times the stake when the die shows it.

The contract's own RAPID balance is the bank that pays the winners, and a bet the bank could not cover is refused before the die is rolled.
The roll comes from random(), whose values the server records with the execution, so anyone can check an outcome afterwards.
A refused bet fails the call, which returns the stake.
"""

from rapidwire_contract import *

MIN_BET = 1_000  # 1.000 RAPID
MAX_BET = 50_000  # 50.000 RAPID
SIDES = 6
PAYOUT_MULTIPLIER = 5


def record(won: bool, payout: int) -> None:
    totals: dict[str, int] = storage.get("stats", {"games": 0, "wins": 0, "paid_out": 0})
    totals["games"] += 1
    if won:
        totals["wins"] += 1
        totals["paid_out"] += payout
    storage["stats"] = totals


@public
def bet(guess: int) -> str:
    rapid = currency("RAPID")
    if paid.currency != rapid:
        revert("Bets are placed in RAPID")
    if paid.amount < MIN_BET or paid.amount > MAX_BET:
        revert(f"Bet between {MIN_BET} and {MAX_BET} minimal units")
    if guess < 1 or guess > SIDES:
        revert(f"Guess a number from 1 to {SIDES}")
    payout = paid.amount * PAYOUT_MULTIPLIER
    # The stake already sits in the contract's balance, so the bank must hold the whole payout including it.
    if balance(self, rapid) < payout:
        revert("The bank cannot cover this bet right now")
    roll = random(1, SIDES)
    won = roll == guess
    record(won, payout)
    if won:
        transfer(sender, rapid, payout)
        return f"rolled {roll}: you win {payout}"
    return f"rolled {roll}: you lose"


@public
def stats() -> str:
    totals: dict[str, int] = storage.get("stats", {"games": 0, "wins": 0, "paid_out": 0})
    return f"{totals['games']} games, {totals['wins']} wins, {totals['paid_out']} paid out"
