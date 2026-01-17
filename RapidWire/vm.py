from typing import Any, List, Dict, Optional, TYPE_CHECKING
import asyncio
import hashlib
import random
import time
from .exceptions import ContractError, TransactionCanceledByContract
from .constants import CONTRACT_OP_COSTS

if TYPE_CHECKING:
    from .core import ContractAPI

class StopExecution(Exception):
    pass

class RapidWireVM:
    def __init__(self, script: List[Dict[str, Any]], api: 'ContractAPI', system_vars: Dict[str, Any]):
        self.script = script
        self.api = api
        self.vars = system_vars
        self.output = None
        self.instruction_count = 0
        self.current_op = None

    def _resolve_arg(self, arg: Any) -> Any:
        if isinstance(arg, str):
            if arg.startswith('_'):
                return self.vars.get(arg)
            if arg.isdigit():
                return int(arg)
        return arg

    def _set_var(self, name: str, value: Any):
        if name and isinstance(name, str) and name.startswith('_'):
            # Security fix: Prevent overwriting system variables
            if name in ['_sender', '_self', '_input']:
                 self._raise_error(f"Cannot overwrite system variable '{name}'.")

            if isinstance(value, int):
                if abs(value) > 10**30:
                    self._raise_error(f"Variable '{name}' exceeded numeric limit.")
            elif isinstance(value, str):
                if len(value) > 127:
                    self._raise_error(f"Variable '{name}' exceeded string length limit.")
            self.vars[name] = value

    def _raise_error(self, message: str):
        raise ContractError(message, instruction=self.instruction_count, op=self.current_op)

    async def run(self):
        try:
            await self._execute_block(self.script)
        except StopExecution:
            pass
        except TransactionCanceledByContract:
            raise
        except ContractError:
            raise
        except Exception as e:
            self._raise_error(str(e))

    async def _execute_block(self, block: List[Dict[str, Any]]):
        for cmd in block:
            self.instruction_count += 1
            op = cmd.get('op')
            self.current_op = op

            # Dynamic Cost Check
            self.api.add_cost(op)

            raw_args = cmd.get('args', [])
            args = [self._resolve_arg(a) for a in raw_args]
            out = cmd.get('out')

            result = await self._execute_op(op, args, cmd)

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

    async def _execute_op(self, op: str, args: List[Any], cmd: Dict[str, Any]) -> Any:
        # A. Calculation & Logic
        if op == 'add': return int(args[0]) + int(args[1])
        if op == 'sub': return int(args[0]) - int(args[1])
        if op == 'mul': return int(args[0]) * int(args[1])
        if op == 'div': return int(args[0]) // int(args[1])
        if op == 'mod': return int(args[0]) % int(args[1])
        if op == 'concat': return str(args[0]) + str(args[1])
        if op == 'eq': return 1 if str(args[0]) == str(args[1]) else 0
        if op == 'neq': return 1 if str(args[0]) != str(args[1]) else 0
        if op == 'gt': return 1 if int(args[0]) > int(args[1]) else 0
        if op == 'lt': return 1 if int(args[0]) < int(args[1]) else 0
        if op == 'gte': return 1 if int(args[0]) >= int(args[1]) else 0
        if op == 'lte': return 1 if int(args[0]) <= int(args[1]) else 0
        if op == 'set': return args[0]

        # B. Flow Control
        if op == 'if':
            condition = args[0]
            if condition:
                await self._execute_block(cmd.get('then', []))
            else:
                await self._execute_block(cmd.get('else', []))
            return None

        if op == 'while':
            raw_cond = cmd.get('args', [])[0]
            body = cmd.get('body', [])
            while True:
                cond_val = self._resolve_arg(raw_cond)
                if not cond_val:
                    break
                await self._execute_block(body)
                self.api.add_cost('while')
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
            return await self.api.transfer(self.vars['_self'], to_id, cur_id, amount)

        if op == 'get_balance':
            # args: [user, cur]
            return await self.api.get_balance(int(args[0]), int(args[1]))

        if op == 'output':
            # args: [message]
            self.output = str(args[0])
            return None

        if op == 'store_get':
            # args: [key]
            key = str(args[0])
            val = await self.api.get_variable(None, key) # None user_id defaults to owner in api
            if val is None: return ""
            return str(val)

        if op == 'store_set':
            # args: [key, val]
            key = str(args[0])
            val = str(args[1])
            await self.api.set_variable(key, val)
            return None

        if op == 'approve':
            # args: [spender, amount, cur]
            spender = int(args[0])
            amount = int(args[1])
            cur_id = int(args[2])
            await self.api.approve(spender, cur_id, amount)
            return None

        if op == 'transfer_from':
            # args: [sender, recipient, amount, cur]
            sender = int(args[0])
            recipient = int(args[1])
            amount = int(args[2])
            cur_id = int(args[3])
            return await self.api.transfer_from(sender, recipient, cur_id, amount)

        if op == 'get_allowance':
            # args: [owner, spender, cur]
            owner = int(args[0])
            spender = int(args[1])
            cur_id = int(args[2])
            return await self.api.get_allowance(owner, spender, cur_id)

        if op == 'get_currency':
            # args: [cur_id]
            return await self.api.get_currency(int(args[0]))

        if op == 'get_transaction':
            # args: [tx_id]
            return await self.api.get_transaction(int(args[0]))

        if op == 'attr':
            # args: [obj, prop]
            obj = args[0]
            prop = args[1]

            if not isinstance(prop, str):
                return None

            if prop.startswith('__'):
                return None

            if isinstance(obj, dict):
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
            payer = int(args[0])
            amount = int(args[1])
            cur = int(args[2])
            desc = str(args[3]) if len(args) > 3 else None
            # ContractAPI.create_claim(self, claimant: int, payer: int, currency: int, amount: int, desc: Optional[str] = None)
            # claimant is self (contract owner)
            return await self.api.create_claim(self.vars['_self'], payer, cur, amount, desc)

        if op == 'pay_claim':
            # args: [claim_id]
            return await self.api.pay_claim(int(args[0]), self.vars['_self'])

        if op == 'cancel_claim':
            # args: [claim_id]
            return await self.api.cancel_claim(int(args[0]), self.vars['_self'])

        if op == 'swap':
            # args: [from_currency_id, to_currency_id, amount]
            return await self.api.swap(int(args[0]), int(args[1]), int(args[2]))

        if op == 'add_liquidity':
            # args: [currency_a_id, currency_b_id, amount_a, amount_b]
            return await self.api.add_liquidity(int(args[0]), int(args[1]), int(args[2]), int(args[3]))

        if op == 'remove_liquidity':
            # args: [currency_a_id, currency_b_id, shares]
            return await self.api.remove_liquidity(int(args[0]), int(args[1]), int(args[2]))

        if op == 'execute':
            # args: [dest, input]
            dest = int(args[0])
            input_data = str(args[1]) if len(args) > 1 else None
            return await self.api.execute_contract(dest, input_data)

        if op == 'discord_send':
            # args: [guild_id, channel_id, message]
            try:
                guild_id = int(args[0])
                channel_id = int(args[1])
                message = str(args[2])
            except (ValueError, IndexError):
                self._raise_error("Invalid arguments for discord_send")

            return 1 if await self.api.discord_send(guild_id, channel_id, message) else 0

        if op == 'discord_role_add':
            # args: [user_id, guild_id, role_id]
            try:
                user_id = int(args[0])
                guild_id = int(args[1])
                role_id = int(args[2])
            except (ValueError, IndexError):
                self._raise_error("Invalid arguments for discord_role_add")

            return 1 if await self.api.discord_role_add(guild_id, user_id, role_id) else 0

        if op == 'has_role':
            # args: [user_id, guild_id, role_id]
            try:
                user_id = int(args[0])
                guild_id = int(args[1])
                role_id = int(args[2])
            except (ValueError, IndexError):
                self._raise_error("Invalid arguments for has_role")

            return 1 if await self.api.has_role(guild_id, user_id, role_id) else 0

        if op == 'sha256':
            # args: [string]
            try:
                s = str(args[0])
                return hashlib.sha256(s.encode('utf-8')).hexdigest()
            except IndexError:
                self._raise_error("Invalid arguments for sha256")

        if op == 'random':
            # args: [min, max]
            try:
                min_val = int(args[0])
                max_val = int(args[1])
                return random.randint(min_val, max_val)
            except (ValueError, IndexError):
                self._raise_error("Invalid arguments for random")

        if op == 'length':
            # args: [obj]
            # Returns len(obj)
            try:
                obj = args[0]
                return len(obj)
            except (TypeError, IndexError):
                self._raise_error("Invalid argument for length")

        if op == 'slice':
            # args: [obj, start, stop, step]
            try:
                obj = args[0]
                # Helper to convert to int or None
                def as_int_or_none(val):
                    if val is None:
                        return None
                    try:
                        return int(val)
                    except ValueError:
                        return None

                start = as_int_or_none(args[1]) if len(args) > 1 else None
                stop = as_int_or_none(args[2]) if len(args) > 2 else None
                step = as_int_or_none(args[3]) if len(args) > 3 else None

                return obj[start:stop:step]
            except (TypeError, IndexError):
                self._raise_error("Invalid arguments for slice")

        if op == 'split':
            # args: [string, separator]
            try:
                s = str(args[0])
                sep = str(args[1])
                return s.split(sep)
            except (IndexError, ValueError):
                self._raise_error("Invalid arguments for split")

        if op == 'to_str':
            # args: [val]
            try:
                return str(args[0])
            except IndexError:
                self._raise_error("Invalid arguments for to_str")

        if op == 'to_int':
            # args: [val]
            try:
                return int(args[0])
            except (IndexError, ValueError):
                self._raise_error("Invalid arguments for to_int")

        if op == 'now':
            # args: []
            return int(time.time())

        # Fallback or Error
        self._raise_error(f"Unknown operation: {op}")
