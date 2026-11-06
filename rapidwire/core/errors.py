"""Domain errors, each with a unique snake_case code and an English message safe to show to users; error_from_code() rebuilds a stored error so an idempotent replay re-raises what the first request raised."""

from __future__ import annotations

from typing import ClassVar

from rapidwire.core.types import ExecutionStatus

_REGISTRY: dict[str, type[DomainError]] = {}


class DomainError(Exception):
    """An expected failure of an operation; services record it on the execution as status failed (or reverted for RevertError)."""

    code: str = "domain_error"
    default_message: ClassVar[str] = "The operation failed."
    status: ClassVar[ExecutionStatus] = ExecutionStatus.FAILED

    def __init__(self, message: str | None = None, *, execution_id: int | None = None) -> None:
        self.message = message if message is not None else self.default_message
        self.execution_id = execution_id
        # Set by the ledger when the error comes from a failed child execution: its type, contract_function, code and message.
        self.child: dict[str, str | None] | None = None
        super().__init__(self.message)

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        code = cls.__dict__.get("code")
        if code is None:
            return
        if code in _REGISTRY:
            raise TypeError(f"domain error code {code!r} is already used by {_REGISTRY[code].__qualname__}")
        _REGISTRY[code] = cls

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r}, execution_id={self.execution_id!r})"


_REGISTRY[DomainError.code] = DomainError


class NotFoundError(DomainError):
    code = "not_found"
    default_message = "The requested item was not found."


class CurrencyNotFound(NotFoundError):
    code = "currency_not_found"
    default_message = "The currency was not found."


class ClaimNotFound(NotFoundError):
    code = "claim_not_found"
    default_message = "The claim was not found."


class AccountNotFound(NotFoundError):
    code = "account_not_found"
    default_message = "The account was not found."


class InsufficientBalance(DomainError):
    code = "insufficient_balance"
    default_message = "The balance is insufficient."


class InsufficientAllowance(DomainError):
    code = "insufficient_allowance"
    default_message = "The allowance is insufficient."


class InsufficientStake(DomainError):
    code = "insufficient_stake"
    default_message = "The staked amount is insufficient."


class InvalidAmount(DomainError):
    code = "invalid_amount"
    default_message = "The amount must be a positive integer number of minimal units."


class InvalidArgument(DomainError):
    code = "invalid_argument"
    default_message = "An argument is invalid."


class PermissionDenied(DomainError):
    code = "permission_denied"
    default_message = "You do not have permission to perform this operation."


class Unauthorized(DomainError):
    """The caller presented no API token, or one that is unknown, revoked or not the initiator's; the API raises it before or instead of recording an execution."""

    code = "unauthorized"
    default_message = "A valid API token is required."


class GuildUnavailable(DomainError):
    code = "guild_unavailable"
    default_message = "The bot is not in this server, so its owner and administrators cannot be determined."


class CurrencyRenounced(DomainError):
    code = "currency_renounced"
    default_message = "Minting has been renounced for this currency."


class TimelockActive(DomainError):
    code = "timelock_active"
    default_message = "The waiting period for this request has not passed yet."


class RequestExpired(DomainError):
    code = "request_expired"
    default_message = "The request has expired."


class NoPendingRequest(DomainError):
    code = "no_pending_request"
    default_message = "There is no pending request."


class AlreadyExists(DomainError):
    code = "already_exists"
    default_message = "It already exists."


class InvalidState(DomainError):
    code = "invalid_state"
    default_message = "The operation is not allowed in the current state."


class AmountOverflow(DomainError):
    code = "amount_overflow"
    default_message = "The resulting amount exceeds the supported range."


class IdempotencyConflict(DomainError):
    code = "idempotency_conflict"
    default_message = "The idempotency key was already used for a different request."


class PoolNotFound(NotFoundError):
    code = "pool_not_found"
    default_message = "The pool was not found."


class PoolAlreadyExists(AlreadyExists):
    code = "pool_already_exists"
    default_message = "A pool already exists for this currency pair and fee tier."


class SlippageExceeded(DomainError):
    code = "slippage_exceeded"
    default_message = "The result fell short of the minimum you accepted."


class InsufficientLiquidity(DomainError):
    code = "insufficient_liquidity"
    default_message = "The pool's liquidity is insufficient for this operation."


class InsufficientShares(DomainError):
    code = "insufficient_shares"
    default_message = "You do not hold enough shares of this pool."


class NoRoute(DomainError):
    """No route pays out anything; zero_output is True when routes exist but the output rounds down to zero, False when no route connects the currencies."""

    code = "no_route"
    default_message = "No swap route connects these currencies."
    # The failure output field recording zero_output, so an error rebuilt by an idempotent replay keeps it.
    ZERO_OUTPUT_FIELD: ClassVar[str] = "zero_output"

    def __init__(self, message: str | None = None, *, execution_id: int | None = None, zero_output: bool = False) -> None:
        super().__init__(message, execution_id=execution_id)
        self._zero_output = zero_output

    @property
    def zero_output(self) -> bool:
        from rapidwire.core.ledger import failure_output

        return self._zero_output or failure_output(self).get(self.ZERO_OUTPUT_FIELD) is True


class ContractNotFound(NotFoundError):
    code = "contract_not_found"
    default_message = "No contract is registered for this account."


class ContractLocked(DomainError):
    code = "contract_locked"
    default_message = "The contract is locked, so it cannot be updated, paused or deleted until the lock ends."


class ContractPaused(DomainError):
    code = "contract_paused"
    default_message = "The contract is paused."


class InvalidContract(DomainError):
    """A binary that failed the registration checks; the message is the verifier's."""

    code = "invalid_contract"
    default_message = "The contract binary is invalid."


class ContractStorageLimit(DomainError):
    code = "contract_storage_limit"
    default_message = "The contract's storage limit would be exceeded."


class RevertError(DomainError):
    """A deliberate cancellation (for example a contract revert); the execution is recorded with status reverted."""

    code = "reverted"
    default_message = "The operation was reverted."
    status = ExecutionStatus.REVERTED


def error_class(code: str) -> type[DomainError] | None:
    """The registered class for a code, or None when no class uses it."""
    return _REGISTRY.get(code)


def error_from_code(code: str, message: str, *, execution_id: int | None = None, child: dict[str, str | None] | None = None) -> DomainError:
    """Rebuild an error from a stored code, message and child details; an unknown code yields a plain DomainError carrying that code."""
    cls = _REGISTRY.get(code)
    if cls is None:
        error = DomainError(message, execution_id=execution_id)
        error.code = code
    else:
        error = cls(message, execution_id=execution_id)
    error.child = dict(child) if child is not None else None
    return error


class ContractExecutionError(DomainError):
    """A contract execution ended with a failure of the contract VM; failure is the VM exception itself (None for an error rebuilt from a stored record), and the failed execution's output records the failure kind."""

    def __init__(self, message: str | None = None, *, execution_id: int | None = None, failure: BaseException | None = None) -> None:
        super().__init__(message, execution_id=execution_id)
        self.failure = failure


class ContractReverted(ContractExecutionError, RevertError):
    """The contract called revert() or failed an assert; the execution is recorded as reverted."""

    code = "contract_reverted"
    default_message = "The contract reverted."


class ContractRuntimeFailure(ContractExecutionError):
    """The contract hit a runtime fault, including a failed builtin operation."""

    code = "contract_runtime_error"
    default_message = "The contract failed at run time."


class ContractOutOfGas(ContractExecutionError):
    code = "contract_out_of_gas"
    default_message = "The contract ran out of gas."


class ContractCallFailed(ContractExecutionError):
    """The contract function could not be called (no such public function, arguments of the wrong types, or a nested call that could not be made)."""

    code = "contract_call_failed"
    default_message = "The contract function could not be called."


class ContractLimitExceeded(ContractExecutionError):
    """The memory or wall-clock limit of the whole top-level contract call was exceeded."""

    code = "contract_limit_exceeded"
    default_message = "The contract exceeded a resource limit."
