"""Members-only club: accounts holding at least MIN_HOLDING VIP, or holding MEMBER_ROLE in GUILD, collect a daily RAPID reward and may post to the club channel.

The rewards come from the contract's own RAPID balance, which the owner tops up with ordinary transfers.
Each member's last collection day is kept in storage under "day:" followed by the account ID, and "collected" holds the total paid out.
An administrator of GUILD must allow the contract with /discord-permission allow, both for the role check and for posting; a post is sent by the worker after the call commits.
"""

from rapidwire_contract import *

GUILD = 345678901234567890
MEMBER_ROLE = 456789012345678901
CLUB_CHANNEL = 567890123456789012
MIN_HOLDING = 10_000  # 10.000 VIP
REWARD = 2_000  # 2.000 RAPID
DAY = 86_400
MAX_POST = 200


def is_member(user: Account) -> bool:
    if balance(user, currency("VIP")) >= MIN_HOLDING:
        return True
    return discord.has_role(GUILD, user, MEMBER_ROLE)


@public
def member(user: Account) -> bool:
    return is_member(user)


@public
def collect() -> int:
    if not is_member(sender):
        revert("Members only: hold 10 VIP or the member role")
    today = now() // DAY
    key = "day:" + str(sender)
    if storage.get(key, -1) >= today:
        revert("Already collected today")
    rapid = currency("RAPID")
    if balance(self, rapid) < REWARD:
        revert("The reward pool is empty")
    storage[key] = today
    storage["collected"] = storage.get("collected", 0) + REWARD
    transfer(sender, rapid, REWARD)
    return REWARD


@public
def post(text: str) -> str:
    if not is_member(sender):
        revert("Members only: hold 10 VIP or the member role")
    text = text.strip()
    if len(text) == 0 or len(text) > MAX_POST:
        revert(f"A post has 1 to {MAX_POST} characters")
    discord.send(GUILD, CLUB_CHANNEL, f"<@{sender}> {text}")
    return "posted"
