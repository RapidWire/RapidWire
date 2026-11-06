"""The gas cost table: the base cost of every opcode and builtin, and the size-dependent surcharges.

Auxiliary instructions (constant loads, local loads and stores, stack shuffles, unconditional jumps, return, record field reads) cost 0; an instruction that corresponds to one operation of the old VM (arithmetic, comparison, conditional branch, loop step, internal call, string and collection operations, conversions) costs 1.
Builtins mirror the old table; builtins the old VM did not have take the cost of their nearest neighbour (min, max and abs cost 1 like arithmetic, Account() and Currency() 1 like a conversion, string and dict helpers 1 like split and getitem, claim_info 2 like get_transaction, stake and unstake 10 like transfer, staked 1 like a balance read, quote 20 like swap, try_call 15 like call).

Gas is charged before an operation runs, so an operation that cannot be paid has no effect.

Loops and runs of free instructions:

- The verifier rejects any loop whose every instruction is free, so each iteration of a loop pays at least one charged instruction; a while loop pays exactly its conditional branch (1) per iteration.
- Free run rule: every free instruction adds its weight (FREE_INSTRUCTION_WEIGHTS, FREE_BUILTIN_WEIGHTS for a free CALL_BUILTIN, otherwise 1) to a counter that each charged instruction and each RET set back to 0, and whenever the counter reaches FREE_RUN_ALLOWANCE, 1 gas is charged and FREE_RUN_ALLOWANCE is taken off the counter.
- The counter starts at 0 in every function invocation, so a function costs the same whether it is the entry of an execution, called with CALL or called from another contract.

Operations whose work grows with the size of their operands add a surcharge computed before the work is done:

- str_gas(n) = n // STR_CHARS_PER_GAS for the characters an operation reads or produces (CONCAT of str, SLICE of str, CONTAINS on str, sha256, the str.* helpers);
- elements_gas(n) = n // ELEMENTS_PER_GAS for the elements an operation copies or scans (CONCAT of lists, SLICE of a list, CONTAINS on a list, BUILD_LIST, BUILD_DICT, positional LIST_POP and DELETE_ITEM on a list, dict.keys, dict.values, str.split and str.join results, the scalar elements CHECK_TYPE inspects); list copies count each element COPY_ELEMENT_WEIGHT times, CONTAINS on a list of str counts each element as 1 + len(needle) // 64 elements, and CONTAINS of a bool, 0 or 1 in a list mixing bools and ints counts each element AMBIGUOUS_CONTAINS_ELEMENT_WEIGHT more times;
- CHECK_TYPE pays 1 more for every nested list, dict or record it inspects;
- storage_write_gas(n, v) for storage.set and storage_read_gas(n, v) for storage.get and read_storage, where n is the encoded size in bytes and v the number of values in it (a read is charged after decoding, which has no effect of its own, and a storage.set that fails pays for the bytes and values it encoded before failing);
- route_search_gas(e, c) for swap and quote, charged by the host on top of their base cost, where e is the number of path expansions the route search simulated and c the chunk-hops of the allocation (the swap chunk count times the total pool count of the candidate paths);
- memory_walk_gas(n) whenever the memory meter measures live memory, n being the references it visits (see vm.MemoryMeter).

Every other operation is bounded by the value limits (str length, collection size, int width), so the time one gas unit buys stays within a small factor of plain arithmetic.
"""

from __future__ import annotations

from collections.abc import Iterable
from types import MappingProxyType

FREE_RUN_ALLOWANCE = 8
STR_CHARS_PER_GAS = 256
ELEMENTS_PER_GAS = 64
STORAGE_WRITE_BYTES_PER_GAS = 32
STORAGE_READ_BYTES_PER_GAS = 128
# A write encodes each value in the VM and decodes and re-encodes it canonically in the host; a read decodes it once.
STORAGE_WRITE_GAS_PER_VALUE = 2
STORAGE_READ_GAS_PER_VALUE = 1
ROUTE_GAS_PER_EXPANSION = 2
# Allocation simulates each chunk through every pool of every candidate path; ROUTE_GAS_PER_TWO_CHUNK_HOPS gas per two such chunk-hops, rounded up.
ROUTE_GAS_PER_TWO_CHUNK_HOPS = 3
WALK_REFERENCES_PER_GAS = 4
# Weights of the free instructions that do more work than a load or store; every weight is at most FREE_RUN_ALLOWANCE.
FREE_INSTRUCTION_WEIGHTS = MappingProxyType({"RET": 3, "PUSH_CONST": 2})
FREE_BUILTIN_WEIGHTS = MappingProxyType({"sender": 2, "self": 2, "paid": 3})
# Operations that copy a list (CONCAT of lists, SLICE of a list, dict.keys, dict.values) also scan the copy for its accounted size, so each element counts this many times.
COPY_ELEMENT_WEIGHT = 2
# In CONTAINS on a list of str, comparing against one element counts as 1 + len(needle) // NEEDLE_CHARS_PER_ELEMENT elements.
NEEDLE_CHARS_PER_ELEMENT = 64
# CONTAINS of True, False, 0 or 1 in a list mixing bools and ints compares types one element at a time and counts each element this many more times.
AMBIGUOUS_CONTAINS_ELEMENT_WEIGHT = 8

OPCODE_GAS = MappingProxyType(
    {
        "PUSH_INT": 0,
        "PUSH_CONST": 0,
        "PUSH_TRUE": 0,
        "PUSH_FALSE": 0,
        "POP": 0,
        "DUP": 0,
        "SWAP": 0,
        "LOAD": 0,
        "STORE": 0,
        "ADD": 1,
        "SUB": 1,
        "MUL": 1,
        "FLOORDIV": 1,
        "MOD": 1,
        "NEG": 1,
        "EQ": 1,
        "NE": 1,
        "LT": 1,
        "LE": 1,
        "GT": 1,
        "GE": 1,
        "NOT": 1,
        "JUMP": 0,
        "JUMP_IF_FALSE": 1,
        "JUMP_IF_TRUE": 1,
        "FOR_RANGE": 1,
        "FOR_EACH": 1,
        "CALL": 1,
        "RET": 0,
        "CALL_BUILTIN": 0,
        "CALL_CONTRACT": 15,
        "TRY_CALL": 15,
        "REVERT": 0,
        "ASSERT": 1,
        "CONCAT": 1,
        "SLICE": 1,
        "INDEX_GET": 1,
        "INDEX_SET": 1,
        "LEN": 1,
        "CONTAINS": 1,
        "BUILD_LIST": 1,
        "BUILD_DICT": 1,
        "APPEND": 1,
        "LIST_POP": 1,
        "DELETE_ITEM": 1,
        "GET_FIELD": 0,
        "CHECK_TYPE": 1,
        "TO_INT": 1,
        "TO_STR": 1,
    }
)
"""Base gas per opcode; CALL_BUILTIN itself is free and charges the builtin's cost from BUILTIN_GAS."""

BUILTIN_GAS = MappingProxyType(
    {
        "sender": 0,
        "self": 0,
        "paid": 0,
        "now": 1,
        "balance": 1,
        "currency": 1,
        "currency_info": 1,
        "transfer": 10,
        "transfer_from": 10,
        "approve": 3,
        "allowance": 1,
        "create_claim": 3,
        "pay_claim": 5,
        "cancel_claim": 2,
        "claim_info": 2,
        "swap": 20,
        "add_liquidity": 15,
        "remove_liquidity": 15,
        "quote": 20,
        "stake": 10,
        "unstake": 10,
        "staked": 1,
        "storage.get": 1,
        "storage.set": 3,
        "storage.delete": 3,
        "read_storage": 1,
        "discord.send": 5,
        "discord.add_role": 10,
        "discord.remove_role": 10,
        "discord.has_role": 1,
        "sha256": 5,
        "random": 2,
        "min": 1,
        "max": 1,
        "abs": 1,
        "Account": 1,
        "Currency": 1,
        "str.split": 1,
        "str.find": 1,
        "str.startswith": 1,
        "str.endswith": 1,
        "str.replace": 1,
        "str.upper": 1,
        "str.lower": 1,
        "str.strip": 1,
        "str.join": 1,
        "dict.get": 1,
        "dict.keys": 1,
        "dict.values": 1,
    }
)
"""Base gas per builtin, charged by CALL_BUILTIN before the builtin runs."""


def str_gas(chars: int) -> int:
    """Surcharge for reading or producing chars characters."""
    return chars // STR_CHARS_PER_GAS


def elements_gas(elements: int) -> int:
    """Surcharge for copying or scanning that many collection elements."""
    return elements // ELEMENTS_PER_GAS


def free_instruction_weight(mnemonic: str, builtin: str | None = None) -> int:
    """Weight of one free instruction under the free run rule; builtin names the builtin of a free CALL_BUILTIN."""
    if builtin is not None:
        return FREE_BUILTIN_WEIGHTS[builtin]
    return FREE_INSTRUCTION_WEIGHTS.get(mnemonic, 1)


def free_run_gas(weights: Iterable[int]) -> int:
    """Gas the free run rule charges for free instructions of these weights executed in order from a counter of 0 with no charged instruction between them."""
    counter = 0
    gas = 0
    for weight in weights:
        counter += weight
        if counter >= FREE_RUN_ALLOWANCE:
            counter -= FREE_RUN_ALLOWANCE
            gas += 1
    return gas


def storage_write_gas(encoded_bytes: int, values: int = 0) -> int:
    """Total gas of storage.set for a key and value whose encoded sizes add up to encoded_bytes and whose value holds that many values (every scalar, list and dict counts one): the base cost, 1 per started STORAGE_WRITE_BYTES_PER_GAS bytes and STORAGE_WRITE_GAS_PER_VALUE per value."""
    return BUILTIN_GAS["storage.set"] + -(-encoded_bytes // STORAGE_WRITE_BYTES_PER_GAS) + values * STORAGE_WRITE_GAS_PER_VALUE


def storage_read_gas(encoded_bytes: int, values: int = 0) -> int:
    """Total gas of storage.get or read_storage when the stored value has encoded_bytes bytes and holds that many values: the base cost, 1 per full STORAGE_READ_BYTES_PER_GAS bytes and STORAGE_READ_GAS_PER_VALUE per value."""
    return BUILTIN_GAS["storage.get"] + encoded_bytes // STORAGE_READ_BYTES_PER_GAS + values * STORAGE_READ_GAS_PER_VALUE


def route_search_gas(expansions: int, chunk_hops: int = 0) -> int:
    """Gas a swap or quote pays on top of its base cost for its route search: ROUTE_GAS_PER_EXPANSION per path expansion (routing.PathSearch.expansions) plus ROUTE_GAS_PER_TWO_CHUNK_HOPS per two chunk-hops of the allocation, rounded up (chunk count times the total pool count of the paths given to routing.allocate)."""
    return expansions * ROUTE_GAS_PER_EXPANSION + -(-chunk_hops * ROUTE_GAS_PER_TWO_CHUNK_HOPS // 2)


def memory_walk_gas(references: int) -> int:
    """Gas of one memory measurement that visits that many references: 1 per started WALK_REFERENCES_PER_GAS references."""
    return -(-references // WALK_REFERENCES_PER_GAS)
