# RapidWire Sample Contracts

This document provides a collection of sample contracts for the RapidWire Discord bot. These examples demonstrate various functionalities of the smart contract system.

## How Contracts Work

When a user with a contract receives a transaction, the contract code is executed in a sandboxed environment. The contract has access to two main objects:

- `tx`: A dictionary containing the details of the incoming transaction:
    - `source`: The user ID of the sender.
    - `dest`: The user ID of the recipient (your user ID).
    - `currency`: The ID of the currency being transferred.
    - `amount`: The amount of currency being transferred (as an integer).
    - `input_data`: Any data attached to the transaction by the sender.
- `api`: An object that provides methods to interact with the RapidWire system.
    - `get_balance(user_id, currency_id)`: Gets the balance of a user.
    - `get_transaction(tx_id)`: Retrieves a specific transaction.
    - `transfer(source, dest, currency, amount)`: Initiates a new transfer. **Note:** The `source` must be the contract's owner.
    - `search_transactions(...)`: Searches for transactions.
    - `get_currency(currency_id)`: Gets details about a currency.
    - `create_claim(...)`, `get_claim(...)`, `pay_claim(...)`, `cancel_claim(...)`: Manage claims.
- `Cancel`: An exception that can be raised to cancel the incoming transaction (`raise Cancel("Reason for cancellation")`).
- `return_message`: A variable that can be set to return a message to the transaction sender.

---

## Sample Contracts

### 1. Auto-Reply Contract

This is a basic contract that sends a "Thank you" message back to the sender.

**Code:**
```python
# Set a return message that will be sent to the transaction's source.
return_message = f"Thank you for the {tx['amount']} coins!"
```

**Explanation:**
- The script accesses the `tx` dictionary to get the `amount` from the incoming transaction.
- It then sets the `return_message` variable. The content of this variable will be sent to the user who initiated the transaction after the transfer is successfully processed.

---

### 2. Conditional Refusal Contract

This contract demonstrates how to refuse a transaction based on a specific condition. In this example, the contract rejects any transaction that includes the `input_data` of "fee".

**Code:**
```python
# Check if the input_data is 'fee'
if tx['input_data'] == 'fee':
    # If it is, cancel the transaction with a message.
    raise Cancel("Transactions with 'fee' as input_data are not accepted.")

return_message = "Transaction accepted."
```

**Explanation:**
- The script checks the `input_data` field of the `tx` object.
- If the `input_data` matches the string "fee", the script raises the `Cancel` exception.
- Raising `Cancel` immediately stops the transaction, and the provided message is sent back to the sender as the reason for cancellation.
- If the condition is not met, the transaction proceeds, and a confirmation message is set.

---

### 3. Tip Forwarding Contract

This contract automatically forwards a percentage of the received funds to another user. This could be used for a "dev tax" or a savings account.

**Code:**
```python
# The user ID of the person to forward the tip to.
# IMPORTANT: Replace 123456789012345678 with the actual Discord User ID.
FORWARD_TO_USER_ID = 123456789012345678

# The percentage of the amount to forward (e.g., 10%)
TIP_PERCENTAGE = 0.10

# Calculate the amount to forward
tip_amount = int(tx['amount'] * TIP_PERCENTAGE)

# Ensure at least 1 unit is forwarded if the percentage is very small
if tip_amount == 0 and tx['amount'] > 0:
    tip_amount = 1

# If there is a tip to send, transfer it.
if tip_amount > 0:
    api.transfer(
        source=tx['dest'],  # The source is always the contract owner
        dest=FORWARD_TO_USER_ID,
        currency=tx['currency'],
        amount=tip_amount
    )
    return_message = f"Thank you! {tip_amount} has been forwarded as a tip."
else:
    return_message = "Thank you for the transaction!"

```

**Explanation:**
- **Configuration**: You must set `FORWARD_TO_USER_ID` to the actual Discord user ID you want to send the funds to. `TIP_PERCENTAGE` can be adjusted as needed.
- **Calculation**: The script calculates the amount to forward based on the `TIP_PERCENTAGE` of the incoming `tx['amount']`. It ensures that at least 1 unit of the currency is sent if the original amount is greater than zero.
- **API Call**: It uses the `api.transfer()` method to send the calculated `tip_amount`.
    - `source`: This **must** be `tx['dest']`, which is the user ID of the account that owns the contract. Contracts can only initiate transfers from their own account.
    - `dest`: The user ID to forward the funds to.
    - `currency`: The same currency as the incoming transaction.
    - `amount`: The calculated tip amount.
- **Return Message**: A message is set to inform the original sender that a portion of their transaction has been forwarded.