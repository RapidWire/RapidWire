"""Role vending machine: pay at least PRICE in RAPID and receive the ROLE role in the Discord server GUILD.

Register the contract on the account that collects the sales, then have an administrator of GUILD allow it with /discord-permission allow so that it may change roles there.
Overpayment is refunded in the same call; a member who already holds the role is turned away, and the failed call returns the payment.
The role change is queued and carried out by the worker after the call commits.
"""

from rapidwire_contract import *

PRICE = 100_000  # 100.000 RAPID in minimal units
GUILD = 123456789012345678
ROLE = 234567890123456789


@public
def buy() -> str:
    if paid.currency != currency("RAPID") or paid.amount < PRICE:
        revert("Send at least 100 RAPID")
    if discord.has_role(GUILD, sender, ROLE):
        revert("You already have the role")
    if paid.amount > PRICE:
        transfer(sender, paid.currency, paid.amount - PRICE)
    discord.add_role(GUILD, sender, ROLE)
    storage["sold"] = storage.get("sold", 0) + 1
    return "OK"


@public
def price() -> int:
    return PRICE


@public
def sold() -> int:
    return storage.get("sold", 0)
