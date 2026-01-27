CONTRACT_OP_COSTS = {
    'add': 1, 'sub': 1, 'mul': 1, 'div': 1, 'mod': 1, 'concat': 1, 'eq': 1, 'neq': 1, 'gt': 1, 'lt': 1, 'gte': 1, 'lte': 1,
    'if': 1, 'while': 1, 'exit': 0, 'cancel': 0,
    'transfer': 10, 'get_balance': 1, 'output': 1,
    'store_get': 1, 'store_set': 3,
    'approve': 3, 'transfer_from': 10,
    'get_currency': 1, 'get_transaction': 2, 'attr': 0,
    'create_claim': 3, 'pay_claim': 5, 'cancel_claim': 2,
    'execute': 15,
    'discord_send': 5, 'discord_role_add': 10, 'has_role': 1,
    'swap': 20, 'add_liquidity': 15, 'remove_liquidity': 15,
    'get_allowance': 1,
    'sha256': 5, 'random': 2, 'length': 1, 'slice': 1, 'split': 1,
    'to_str': 1, 'to_int': 1, 'now': 1,
    'set': 1, 'getitem': 1
}

SYSTEM_USER_ID = 0
SECONDS_IN_A_DAY = 86400
SECONDS_IN_AN_HOUR = 3600

# Interest Rate Scaling
# 1000000 means 100%
# 10000 means 1%
# 1 means 0.0001%
INTEREST_RATE_SCALE = 1000000

MAX_VM_MEMORY = 8192
