class Config:
    class Contract:
        max_cost: int = 100

    class Staking:
        rate_change_timelock: int = 604800 # 7 days

    class Swap:
        fee: int = 30
