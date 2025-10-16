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
    def __init__(self, exc_info: dict[str, str]):
        self.exc_info = exc_info

    def __str__(self):
        return str(self.exc_info)
    
    def __repr__(self):
        return self.__str__()

class DuplicateEntryError(RapidWireError):
    """Raised when trying to insert a duplicate entry into the database."""
    pass

class TransactionCanceledByContract(RapidWireError):
    """Raised when a contract explicitly cancels a transaction."""
    pass
