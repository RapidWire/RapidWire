from typing import Any, List, Dict, Optional, TYPE_CHECKING
import asyncio
import hashlib
import random
from .exceptions import ContractError, TransactionCanceledByContract

if TYPE_CHECKING:
    from .core import ContractAPI

class StopExecution(Exception):
    pass

class RapidWireVM:
    def __init__(self, script: List[Dict[str, Any]], api: 'ContractAPI', system_vars: Dict[str, Any]):
        self.script = script
        self.api = api
        self.vars = system_vars
        self.return_message = None

    def _resolve_arg(self, arg: Any) -> Any:
        if isinstance(arg, str):
            if arg.startswith('_'):
                return self.vars.get(arg)
            if arg.isdigit():
                return int(arg)
        return arg

    def _set_var(self, name: str, value: Any):
        if name and isinstance(name, str) and name.startswith('_'):
            if isinstance(value, (int, float)):
                if abs(value) > 10**30:
                    raise ContractError(f"Variable '{name}' exceeded numeric limit.")
            elif isinstance(value, str):
                if len(value) > 127:
                    raise ContractError(f"Variable '{name}' exceeded string length limit.")
            self.vars[name] = value

    def run(self):
        try:
            self._execute_block(self.script)
        except StopExecution:
            pass

    def _execute_block(self, block: List[Dict[str, Any]]):
        for cmd in block:
            op = cmd.get('op')
            raw_args = cmd.get('args', [])
            args = [self._resolve_arg(a) for a in raw_args]
            out = cmd.get('out')

            result = self._execute_op(op, args, cmd)

            if out:
                self._set_var(out, result)

    @staticmethod
    def _run_async(coro):
        """
        Helper to run async code from sync context if possible.
        Schedules the coroutine as a Task on the running loop.
        Returns the Task object if successful, None otherwise.
        """
        try:
            loop = asyncio.get_running_loop()
            return asyncio.create_task(coro)
        except RuntimeError:
            return None

    def _execute_op(self, op: str, args: List[Any], cmd: Dict[str, Any]) -> Any:
        # Helper to ensure numbers
        def to_num(x):
            try:
                return int(x)
            except (ValueError, TypeError):
                try:
                    return float(x)
                except (ValueError, TypeError):
                    return 0

        # A. Calculation & Logic
        if op == 'add': return to_num(args[0]) + to_num(args[1])
        if op == 'sub': return to_num(args[0]) - to_num(args[1])
        if op == 'mul': return to_num(args[0]) * to_num(args[1])
        if op == 'div': return to_num(args[0]) // to_num(args[1])
        if op == 'mod': return to_num(args[0]) % to_num(args[1])
        if op == 'concat': return str(args[0]) + str(args[1])
        if op == 'eq': return 1 if args[0] == args[1] else 0
        if op == 'gt': return 1 if to_num(args[0]) > to_num(args[1]) else 0
        if op == 'set': return args[0]

        # B. Flow Control
        if op == 'if':
            condition = args[0]
            if condition:
                self._execute_block(cmd.get('then', []))
            else:
                self._execute_block(cmd.get('else', []))
            return None

        if op == 'exit':
            raise StopExecution()

        if op == 'cancel':
            message = args[0] if args else "Transaction canceled"
            raise TransactionCanceledByContract(message)

        # C. RapidWire Actions & Transactions
        if op == 'transfer':
            # args: [to, amount, cur]
            to_id = int(args[0])
            amount = int(args[1])
            cur_id = int(args[2])
            # source is contract owner (self)
            return self.api.transfer(self.vars['_self'], to_id, cur_id, amount)

        if op == 'get_balance':
            # args: [user, cur]
            return self.api.get_balance(int(args[0]), int(args[1]))

        if op == 'reply':
            # args: [message]
            self.return_message = str(args[0])
            return None

        if op == 'store_str_get':
            # args: [key]
            key = str(args[0])
            val = self.api.get_variable(None, key) # None user_id defaults to owner in api
            if val is None: return ""
            return str(val)

        if op == 'store_int_get':
            # args: [key]
            key = str(args[0])
            val = self.api.get_variable(None, key) # None user_id defaults to owner in api
            if val is None: return 0
            return to_num(val)

        if op == 'store_str_set':
            # args: [key, val]
            key = str(args[0])
            val = str(args[1])
            self.api.set_variable(key, val)
            return None

        if op == 'store_int_set':
            # args: [key, val]
            key = str(args[0])
            val = to_num(args[1])
            self.api.set_variable(key, val)
            return None

        if op == 'approve':
            # args: [spender, amount, cur]
            spender = int(args[0])
            amount = int(args[1])
            cur_id = int(args[2])
            self.api.approve(spender, cur_id, amount)
            return None

        if op == 'transfer_from':
            # args: [sender, recipient, amount, cur]
            sender = int(args[0])
            recipient = int(args[1])
            amount = int(args[2])
            cur_id = int(args[3])
            return self.api.transfer_from(sender, recipient, cur_id, amount)

        if op == 'get_currency':
            # args: [cur_id]
            return self.api.get_currency(int(args[0]))

        if op == 'get_transaction':
            # args: [tx_id]
            return self.api.get_transaction(int(args[0]))

        if op == 'attr':
            # args: [obj, prop]
            obj = args[0]
            prop = args[1]
            if hasattr(obj, prop):
                return getattr(obj, prop)
            elif isinstance(obj, dict):
                return obj.get(prop)
            return None

        if op == 'getitem':
            # args: [obj, index]
            obj = args[0]
            idx = args[1]
            try:
                if isinstance(idx, str) and idx.isdigit():
                    idx = int(idx)
                return obj[idx]
            except:
                return None

        if op == 'create_claim':
            # args: [payer, amount, cur, desc]
            # Spec says: ["請求先ID", "金額", "通貨ID", "説明"]
            payer = int(args[0])
            amount = int(args[1])
            cur = int(args[2])
            desc = str(args[3]) if len(args) > 3 else None
            # ContractAPI.create_claim(self, claimant: int, payer: int, currency: int, amount: int, desc: Optional[str] = None)
            # claimant is self (contract owner)
            return self.api.create_claim(self.vars['_self'], payer, cur, amount, desc)

        if op == 'pay_claim':
            # args: [claim_id]
            return self.api.pay_claim(int(args[0]), self.vars['_self'])

        if op == 'cancel_claim':
            # args: [claim_id]
            return self.api.cancel_claim(int(args[0]), self.vars['_self'])

        if op == 'swap':
            # args: [from_currency_id, to_currency_id, amount]
            return self.api.swap(int(args[0]), int(args[1]), int(args[2]))

        if op == 'add_liquidity':
            # args: [currency_a_id, currency_b_id, amount_a, amount_b]
            return self.api.add_liquidity(int(args[0]), int(args[1]), int(args[2]), int(args[3]))

        if op == 'remove_liquidity':
            # args: [currency_a_id, currency_b_id, shares]
            return self.api.remove_liquidity(int(args[0]), int(args[1]), int(args[2]))

        if op == 'exec':
            # args: [dest, input]
            dest = int(args[0])
            input_data = str(args[1]) if len(args) > 1 else None
            return self.api.execute_contract(dest, input_data)

        if op == 'discord_send':
            # args: [guild_id, channel_id, message]
            try:
                guild_id = int(args[0])
                channel_id = int(args[1])
                message = str(args[2])
            except (ValueError, IndexError):
                raise ContractError("Invalid arguments for discord_send")

            # We schedule this as a task. Return 1 (success) optimistically.
            self._run_async(self.api.discord_send(guild_id, channel_id, message))
            return 1

        if op == 'discord_role_add':
            # args: [user_id, guild_id, role_id]
            try:
                user_id = int(args[0])
                guild_id = int(args[1])
                role_id = int(args[2])
            except (ValueError, IndexError):
                raise ContractError("Invalid arguments for discord_role_add")

            # We schedule this as a task. Return 1 (success) optimistically.
            self._run_async(self.api.discord_role_add(guild_id, user_id, role_id))
            return 1

        if op == 'hash':
            # args: [string]
            try:
                s = str(args[0])
                return hashlib.sha256(s.encode('utf-8')).hexdigest()
            except IndexError:
                raise ContractError("Invalid arguments for hash")

        if op == 'random':
            # args: [min, max]
            try:
                min_val = int(args[0])
                max_val = int(args[1])
                return random.randint(min_val, max_val)
            except (ValueError, IndexError):
                raise ContractError("Invalid arguments for random")

        # Fallback or Error
        raise ContractError(f"Unknown operation: {op}")
