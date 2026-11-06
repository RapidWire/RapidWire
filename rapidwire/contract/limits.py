"""Resource limits of contract verification and execution, as one frozen dataclass whose defaults are the production values."""

from __future__ import annotations

from dataclasses import dataclass, field

# Default largest absolute value of an int is 2**INT_BITS - 1.
INT_BITS = 256
STR_MAX_LENGTH = 4096
COLLECTION_MAX_LENGTH = 1024
MEMORY_MAX_BYTES = 2 * 1024 * 1024
MAX_STACK_DEPTH = 128
MAX_LOCALS = 256
MAX_PARAMS = 16
MAX_FRAMES = 64
MAX_CONTRACT_DEPTH = 6
WALL_CLOCK_SECONDS = 2.0
MESSAGE_MAX_LENGTH = 256
STORAGE_KEY_MAX_LENGTH = 64
STORAGE_VALUE_MAX_BYTES = 4096
STORAGE_VALUE_MAX_DEPTH = 16
MAX_RANDOM_DRAWS = 256
# Discord guild, channel, role and user ids are snowflakes in this range.
SNOWFLAKE_MIN = 1
SNOWFLAKE_MAX = 2**63 - 1

# The interpreter compares the wall clock with the deadline after this many instructions, and before every host call.
DEADLINE_CHECK_INTERVAL = 1024


@dataclass(frozen=True, slots=True)
class Limits:
    """Limits applied to one top-level execution and every nested call in it; every value is checked when it is produced, so an oversized value never becomes visible to a contract.

    - int_bits: every int satisfies abs(v) < 2**int_bits.
    - str_max_length: characters (code points) per str.
    - collection_max_length: elements per list and entries per dict.
    - memory_max_bytes: accounted bytes of everything reachable from the frames of all executions of the top-level call (see vm.MemoryMeter for the size model).
    - max_stack_depth: operand stack depth per frame, enforced by the verifier.
    - max_locals and max_params: per function, enforced by the verifier.
    - max_frames: internal call frames per contract execution.
    - max_contract_depth: contract executions on the call stack, counting the top-level one.
    - wall_clock_seconds: monotonic seconds per top-level execution, shared with nested calls.
    - message_max_length: characters of a failure message and of CallResult.message.
    - storage_key_max_length, storage_value_max_bytes and storage_value_max_depth: one storage key in characters, one encoded storage value in bytes, and its list/dict nesting depth.
    - max_random_draws: random() calls per contract execution (each nested call has its own count); the next call is a runtime error.
    """

    int_bits: int = INT_BITS
    str_max_length: int = STR_MAX_LENGTH
    collection_max_length: int = COLLECTION_MAX_LENGTH
    memory_max_bytes: int = MEMORY_MAX_BYTES
    max_stack_depth: int = MAX_STACK_DEPTH
    max_locals: int = MAX_LOCALS
    max_params: int = MAX_PARAMS
    max_frames: int = MAX_FRAMES
    max_contract_depth: int = MAX_CONTRACT_DEPTH
    wall_clock_seconds: float = WALL_CLOCK_SECONDS
    message_max_length: int = MESSAGE_MAX_LENGTH
    storage_key_max_length: int = STORAGE_KEY_MAX_LENGTH
    storage_value_max_bytes: int = STORAGE_VALUE_MAX_BYTES
    storage_value_max_depth: int = STORAGE_VALUE_MAX_DEPTH
    max_random_draws: int = MAX_RANDOM_DRAWS
    int_bound: int = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        for name in (
            "int_bits",
            "str_max_length",
            "collection_max_length",
            "memory_max_bytes",
            "max_stack_depth",
            "max_locals",
            "max_params",
            "max_frames",
            "max_contract_depth",
            "message_max_length",
            "storage_key_max_length",
            "storage_value_max_bytes",
            "storage_value_max_depth",
            "max_random_draws",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive int")
        if not 64 <= self.int_bits <= 4096:
            raise ValueError("int_bits must be between 64 (every inline operand fits) and 4096 (str() of an int stays cheap)")
        if self.max_locals < self.max_params:
            raise ValueError("max_locals must not be less than max_params")
        if not self.wall_clock_seconds > 0:
            raise ValueError("wall_clock_seconds must be positive")
        object.__setattr__(self, "int_bound", 1 << self.int_bits)

    def int_ok(self, value: int) -> bool:
        """Whether value is within the int limit."""
        return -self.int_bound < value < self.int_bound


DEFAULT_LIMITS = Limits()
