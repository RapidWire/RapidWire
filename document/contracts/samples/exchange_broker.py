"""Fixed-rate exchange desk: sells GOLD for RAPID at ASK RAPID per GOLD and buys GOLD back at BID_NUM / BID_DEN RAPID per GOLD.

Sales are filled from the desk's own GOLD inventory; when the inventory is short, the desk buys GOLD on the DEX with the customer's payment, with min_out set to the amount the fixed rate owes, so the customer always receives the quoted amount or the whole call fails and returns the payment.
Any GOLD the swap returns above the owed amount stays with the desk as inventory.
Purchases from customers are paid from the desk's RAPID balance only.
Amounts are minimal units and every conversion rounds down in the desk's favour.
"""

from rapidwire_contract import *

ASK = 3  # the customer pays 3 RAPID per GOLD
BID_NUM = 5  # the desk pays 5 / 2 = 2.5 RAPID per GOLD
BID_DEN = 2


def gold_for(rapid_amount: int) -> int:
    return rapid_amount // ASK


def rapid_for(gold_amount: int) -> int:
    return gold_amount * BID_NUM // BID_DEN


@public
def quote_buy(rapid_amount: int) -> int:
    return gold_for(rapid_amount)


@public
def quote_sell(gold_amount: int) -> int:
    return rapid_for(gold_amount)


@public
def buy() -> int:
    rapid = currency("RAPID")
    gold = currency("GOLD")
    if paid.currency != rapid:
        revert("Pay in RAPID to buy GOLD")
    owed = gold_for(paid.amount)
    if owed == 0:
        revert(f"Pay at least {ASK} RAPID minimal units")
    if balance(self, gold) < owed:
        # swap() fails the call when it would pay out less than min_out, so the customer is never short-changed.
        swap(rapid, gold, paid.amount, owed)
    transfer(sender, gold, owed)
    return owed


@public
def sell() -> int:
    rapid = currency("RAPID")
    gold = currency("GOLD")
    if paid.currency != gold:
        revert("Pay in GOLD to sell it")
    owed = rapid_for(paid.amount)
    if balance(self, rapid) < owed:
        revert("The desk is out of RAPID; try a smaller amount")
    transfer(sender, rapid, owed)
    return owed


@public
def inventory() -> str:
    return f"{balance(self, currency('GOLD'))} GOLD, {balance(self, currency('RAPID'))} RAPID"
