class Config:
    class Contract:
        max_cost: int = 100
        max_script_length: int = 10000
        max_script_size: int = 4096
        max_recursion_depth: int = 6

    class Staking:
        rate_change_timelock: int = 604800 # 7 days

    class Swap:
        fee: int = 30

    class Gas:
        currency_id: int = 1
        price: int = 1

    class Discord:
        token: str = ""

    decimal_places: int = 3
