import unittest
import time
import asyncio
from unittest.mock import MagicMock, AsyncMock
from RapidWire.vm import RapidWireVM, StopExecution
from RapidWire.exceptions import TransactionCanceledByContract, ContractError
from RapidWire.structs import ChainContext

class TestRapidWireVM(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.api = AsyncMock()
        self.chain_context = ChainContext(total_cost=0, budget=100)
        self.api.chain_context = self.chain_context

        # Mock add_cost to update chain_context
        # Since add_cost is synchronous in VM (CPU bound), we keep it sync but maybe mocked
        # However, api is AsyncMock, so methods are async by default.
        # But `add_cost` in `RapidWireVM` calls `self.api.add_cost(op)`.
        # In `RapidWire/core.py`, `add_cost` is synchronous.
        # In `RapidWire/vm.py`, we call it without await: `self.api.add_cost(op)`.
        # So we should mock it as a standard Mock, not AsyncMock for this specific method if possible,
        # or just let it be sync.

        # Let's override add_cost to be a regular Mock since it's called synchronously in VM
        self.api.add_cost = MagicMock()

        def add_cost_side_effect(op):
            self.chain_context.total_cost += 1
            if self.chain_context.total_cost > self.chain_context.budget:
                raise ContractError("Execution budget exceeded.")

        self.api.add_cost.side_effect = add_cost_side_effect

        self.system_vars = {
            '_sender': 100,
            '_self': 200,
            '_input': "test_input"
        }

    async def test_arithmetic(self):
        script = [
            {"op": "add", "args": ["10", "20"], "out": "_res"},
            {"op": "sub", "args": ["_res", "5"], "out": "_res2"},
        ]
        vm = RapidWireVM(script, self.api, self.system_vars)
        await vm.run()
        self.assertEqual(vm.vars['_res'], 30)
        self.assertEqual(vm.vars['_res2'], 25)

    async def test_arithmetic_cast(self):
        script = [
            {"op": "add", "args": ["10", "20"], "out": "_res"},
            {"op": "store_set", "args": ["temp", "_res"]}, # Store 30
            {"op": "store_get", "args": ["temp"], "out": "_val"}, # Get 30
            {"op": "add", "args": ["_val", "5"], "out": "_final"} # Should be 30 + 5 = 35
        ]
        # Mock api response for store_get
        async def side_effect(user, key):
            if key == 'temp':
                return '30'
            return None
        self.api.get_variable.side_effect = side_effect

        vm = RapidWireVM(script, self.api, self.system_vars)
        await vm.run()
        self.assertEqual(vm.vars['_final'], 35)

    async def test_getitem(self):
        script = [
            {"op": "getitem", "args": ["_my_list", "1"], "out": "_res1"},
            {"op": "getitem", "args": ["_my_dict", "key"], "out": "_res2"}
        ]
        self.system_vars['_my_list'] = ["a", "b", "c"]
        self.system_vars['_my_dict'] = {"key": "value"}

        vm = RapidWireVM(script, self.api, self.system_vars)
        await vm.run()
        self.assertEqual(vm.vars['_res1'], "b")
        self.assertEqual(vm.vars['_res2'], "value")

    async def test_flow_control(self):
        script = [
            {"op": "eq", "args": ["_input", "test_input"], "out": "_is_match"},
            {
                "op": "if",
                "args": ["_is_match"],
                "then": [
                    {"op": "output", "args": ["Matched"]}
                ],
                "else": [
                    {"op": "output", "args": ["Not Matched"]}
                ]
            }
        ]
        vm = RapidWireVM(script, self.api, self.system_vars)
        await vm.run()
        self.assertEqual(vm.output, "Matched")

    async def test_transfer(self):
        script = [
            {"op": "transfer", "args": ["300", "10", "1"]}
        ]
        vm = RapidWireVM(script, self.api, self.system_vars)
        await vm.run()
        self.api.transfer.assert_called_with(200, 300, 1, 10) # 200 is source (self), 300 dest, 1 cur, 10 amount

    async def test_cancel(self):
        script = [
            {"op": "cancel", "args": ["Error"]}
        ]
        vm = RapidWireVM(script, self.api, self.system_vars)
        with self.assertRaises(TransactionCanceledByContract):
            await vm.run()

    async def test_stop_execution(self):
        script = [
            {"op": "exit"},
            {"op": "output", "args": ["Should not run"]}
        ]
        vm = RapidWireVM(script, self.api, self.system_vars)
        await vm.run()
        self.assertIsNone(vm.output)

    async def test_store_ops_str(self):
        script = [
            {"op": "store_set", "args": ["key", "value"]},
            {"op": "store_get", "args": ["key"], "out": "_val"}
        ]
        self.api.get_variable.return_value = 'value'
        vm = RapidWireVM(script, self.api, self.system_vars)
        await vm.run()
        self.api.set_variable.assert_called_with('key', 'value')
        self.assertEqual(vm.vars['_val'], 'value')

    async def test_store_ops_int(self):
        script = [
            {"op": "store_set", "args": ["key", 123]},
            {"op": "store_get", "args": ["key"], "out": "_val"}
        ]
        self.api.get_variable.return_value = 123
        vm = RapidWireVM(script, self.api, self.system_vars)
        await vm.run()
        self.api.set_variable.assert_called_with('key', '123')
        self.assertEqual(vm.vars['_val'], '123')

    async def test_sha256(self):
        script = [
            {"op": "sha256", "args": ["hello"], "out": "_res"}
        ]
        vm = RapidWireVM(script, self.api, self.system_vars)
        await vm.run()
        # "hello" sha256: 2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824
        self.assertEqual(vm.vars['_res'], "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824")

    async def test_random(self):
        script = [
            {"op": "random", "args": ["1", "10"], "out": "_res"}
        ]
        vm = RapidWireVM(script, self.api, self.system_vars)
        await vm.run()
        self.assertTrue(1 <= vm.vars['_res'] <= 10)

    async def test_get_allowance(self):
        script = [
            {"op": "get_allowance", "args": ["100", "200", "1"], "out": "_res"}
        ]
        self.api.get_allowance.return_value = 500
        vm = RapidWireVM(script, self.api, self.system_vars)
        await vm.run()
        self.api.get_allowance.assert_called_with(100, 200, 1)
        self.assertEqual(vm.vars['_res'], 500)

    async def test_split(self):
        script = [
            {"op": "split", "args": ["a,b,c", ","], "out": "_res"},
            {"op": "getitem", "args": ["_res", 1], "out": "_val"}
        ]
        vm = RapidWireVM(script, self.api, self.system_vars)
        await vm.run()
        self.assertEqual(vm.vars['_res'], ["a", "b", "c"])
        self.assertEqual(vm.vars['_val'], "b")

    async def test_split_default(self):
        script = [
            {"op": "split", "args": ["hello world", " "], "out": "_res"}
        ]
        vm = RapidWireVM(script, self.api, self.system_vars)
        await vm.run()
        self.assertEqual(vm.vars['_res'], ["hello", "world"])

    async def test_to_str(self):
        script = [
            {"op": "to_str", "args": [123], "out": "_res"}
        ]
        vm = RapidWireVM(script, self.api, self.system_vars)
        await vm.run()
        self.assertEqual(vm.vars['_res'], "123")
        self.assertIsInstance(vm.vars['_res'], str)

    async def test_to_int(self):
        script = [
            {"op": "to_int", "args": ["456"], "out": "_res"}
        ]
        vm = RapidWireVM(script, self.api, self.system_vars)
        await vm.run()
        self.assertEqual(vm.vars['_res'], 456)
        self.assertIsInstance(vm.vars['_res'], int)

    async def test_now(self):
        script = [
            {"op": "now", "out": "_res"}
        ]
        vm = RapidWireVM(script, self.api, self.system_vars)
        start_time = int(time.time())
        await vm.run()
        end_time = int(time.time())
        self.assertIsInstance(vm.vars['_res'], int)
        self.assertTrue(start_time <= vm.vars['_res'] <= end_time)

    async def test_while_loop(self):
        # i = 0
        # while i < 5:
        #   i = i + 1
        script = [
            # i = 0
            {"op": "set", "args": [0], "out": "_i"},
            # Condition check: i < 5
            {"op": "gt", "args": [5, "_i"], "out": "_cond"},
            {
                "op": "while",
                "args": ["_cond"],
                "body": [
                    # i = i + 1
                    {"op": "add", "args": ["_i", 1], "out": "_i"},
                    # Recheck condition: i < 5
                    {"op": "gt", "args": [5, "_i"], "out": "_cond"}
                ]
            }
        ]

        vm = RapidWireVM(script, self.api, self.system_vars)
        await vm.run()

        self.assertEqual(vm.vars['_i'], 5)
        self.assertGreater(self.chain_context.total_cost, 10)
        self.assertLess(self.chain_context.total_cost, 30)

    async def test_infinite_loop_budget_exceeded(self):
        # while 1: pass
        script = [
            {"op": "set", "args": [1], "out": "_cond"},
            {
                "op": "while",
                "args": ["_cond"],
                "body": [
                    # do nothing, just burn gas
                    {"op": "add", "args": [1, 1], "out": "_dump"}
                ]
            }
        ]

        # Low budget
        self.chain_context.budget = 10
        vm = RapidWireVM(script, self.api, self.system_vars)

        with self.assertRaises(ContractError) as cm:
            await vm.run()

        self.assertIn("Execution budget exceeded", str(cm.exception))

if __name__ == '__main__':
    unittest.main()
