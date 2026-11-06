"""Exception types of the contract system.

VerificationError (with its subclass FormatError for unparseable bytes) rejects a binary at registration.
ContractFailure and its subclasses are the defined outcomes of a failing execution; everything else raised through the VM (database errors, ledger rerun requests, HostProtocolError) is a server-side condition that propagates unchanged.
"""

from __future__ import annotations

from enum import StrEnum

from rapidwire.contract.limits import MESSAGE_MAX_LENGTH


def bound_message(message: str, max_length: int = MESSAGE_MAX_LENGTH) -> str:
    """Return message cut to max_length characters, with a trailing "..." when it was cut."""
    if len(message) <= max_length:
        return message
    if max_length <= 3:
        return message[:max_length]
    return message[: max_length - 3] + "..."


def ascii_safe(text: str) -> str:
    """Return text with every non-ASCII or non-printable character backslash-escaped."""
    out = []
    for char in text:
        code = ord(char)
        if 0x20 <= code < 0x7F:
            out.append(char)
        elif code <= 0xFF:
            out.append(f"\\x{code:02x}")
        elif code <= 0xFFFF:
            out.append(f"\\u{code:04x}")
        else:
            out.append(f"\\U{code:08x}")
    return "".join(out)


class ContractError(Exception):
    """Base class of every exception type defined by the contract package."""


class VerificationError(ContractError):
    """The binary does not pass the registration checks."""


class FormatError(VerificationError):
    """The bytes cannot be parsed as a contract binary."""


class ErrorKind(StrEnum):
    """The error field of a CallResult; "" means success."""

    REVERTED = "REVERTED"
    RUNTIME = "RUNTIME"
    OUT_OF_GAS = "OUT_OF_GAS"
    CALL_FAILED = "CALL_FAILED"


class ContractFailure(ContractError):
    """A defined failure of a contract execution.

    message keeps the contract's text, bounded to MESSAGE_MAX_LENGTH characters and further to Limits.message_max_length when it leaves the VM, and str() of the exception is its ASCII-escaped form.
    code is the failure code recorded for the execution, kind the CallResult error a try_call reports, and catchable tells whether try_call can catch it at all.
    function and offset locate the instruction that failed (the innermost execution's function name and byte offset), or are None for a failure outside any instruction.
    """

    kind: ErrorKind = ErrorKind.RUNTIME
    catchable: bool = True

    def __init__(self, message: str) -> None:
        self.message = bound_message(message)
        self.function: str | None = None
        self.offset: int | None = None
        super().__init__(ascii_safe(self.message))

    @property
    def code(self) -> str:
        return self.kind.value

    def bound_to(self, max_length: int) -> None:
        """Cut message to max_length characters."""
        if len(self.message) > max_length:
            self.message = bound_message(self.message, max_length)
            self.args = (ascii_safe(self.message),)


class ContractRevert(ContractFailure):
    """revert() or a failed assert."""

    kind = ErrorKind.REVERTED


class ContractRuntimeError(ContractFailure):
    """A runtime fault: division by zero, type mismatch, index or key missing, a value limit exceeded, a bad operand, or a failed host builtin (host_code is then the HostError code)."""

    kind = ErrorKind.RUNTIME

    def __init__(self, message: str, *, host_code: str | None = None) -> None:
        super().__init__(message)
        self.host_code = host_code


class OutOfGas(ContractFailure):
    """A gas limit ran out; level is the index of the exhausted limit on the gas meter's stack, 0 being the top-level limit."""

    kind = ErrorKind.OUT_OF_GAS

    def __init__(self, message: str, *, level: int) -> None:
        super().__init__(message)
        self.level = level

    @property
    def sublimit(self) -> bool:
        """True when a call's gas sub-limit ran out, False when the top-level limit did."""
        return self.level > 0


class CallFailure(StrEnum):
    """Why a contract call could not be made."""

    NO_CONTRACT = "NO_CONTRACT"
    INACTIVE = "INACTIVE"
    NO_FUNCTION = "NO_FUNCTION"
    BAD_ARGUMENTS = "BAD_ARGUMENTS"
    INSUFFICIENT_BALANCE = "INSUFFICIENT_BALANCE"
    DEPTH = "DEPTH"
    REENTRANCY = "REENTRANCY"
    NO_CURRENCY = "NO_CURRENCY"


class CallFailed(ContractFailure):
    """The call itself could not be made; reason says why."""

    kind = ErrorKind.CALL_FAILED

    def __init__(self, reason: CallFailure, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class LimitResource(StrEnum):
    MEMORY = "MEMORY"
    WALL_CLOCK = "WALL_CLOCK"
    HOST_TIME = "HOST_TIME"


class LimitExceeded(ContractFailure):
    """The memory, wall-clock or host-time limit of the whole top-level execution was exceeded; try_call never catches it."""

    catchable = False

    def __init__(self, resource: LimitResource, message: str) -> None:
        super().__init__(message)
        self.resource = resource

    @property
    def code(self) -> str:
        return f"{self.resource.value}_LIMIT"


class HostError(Exception):
    """Raised by a Host method when a builtin fails by a ledger rule (for example an insufficient balance); the VM turns it into a ContractRuntimeError."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class HostProtocolError(TypeError):
    """A Host method returned a value of the wrong type; this is a server bug and propagates unchanged."""


def call_result_error(exc: ContractFailure) -> ErrorKind:
    """The CallResult error kind that a try_call reports for exc."""
    return exc.kind


def try_call_catches(exc: ContractFailure, gas_level: int) -> bool:
    """Whether a try_call made while the gas meter held gas_level limits catches exc: never a LimitExceeded, and an OutOfGas only when the exhausted limit was opened by that try_call or inside it."""
    if not exc.catchable:
        return False
    if isinstance(exc, OutOfGas):
        return exc.level >= gas_level
    return True
