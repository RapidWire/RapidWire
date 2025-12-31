class RapidWireError(Exception):
    """Base exception class for RapidWire."""
    pass

class UserNotFound(RapidWireError):
    """Raised when a user is not found in the database."""
    pass

class CurrencyNotFound(RapidWireError):
    """Raised when a currency is not found in the database."""
    pass

class InsufficientFunds(RapidWireError):
    """Raised when a user has insufficient funds for a transaction."""
    pass

class TransactionError(RapidWireError):
    """Raised for general transaction failures."""
    pass

class ContractError(RapidWireError):
    """Raised for general contract failures."""
    def __init__(self, message: str, instruction: int = None, op: str = None):
        self.message = message
        self.instruction = instruction
        self.op = op
        super().__init__(message)

    def __str__(self):
        if self.instruction is not None:
            error_msg = f"Error at instruction {self.instruction}"
            if self.op:
                error_msg += f" (op: {self.op})"
            if self.message:
                error_msg += f": {self.message}"
            return error_msg

        return self.message
    
    def __repr__(self):
        return self.__str__()

class DuplicateEntryError(RapidWireError):
    """Raised when trying to insert a duplicate entry into the database."""
    pass

class TransactionCanceledByContract(RapidWireError):
    """Raised when a contract explicitly cancels a transaction."""
    pass

class TimeLockNotExpired(RapidWireError):
    """Raised when a time-locked action is attempted before the lock expires."""
    pass

class RequestExpired(RapidWireError):
    """Raised when a request has expired."""
    pass

class RenouncedError(RapidWireError):
    """Raised when an action is attempted on a renounced feature."""
    pass
