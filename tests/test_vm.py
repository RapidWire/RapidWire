import unittest
import time
import asyncio
from unittest.mock import MagicMock, AsyncMock

import sys
from pathlib import Path
parent_dir = Path(__file__).resolve().parent.parent
sys.path.append(str(parent_dir))

from RapidWire.vm import RapidWireVM, StopExecution
from RapidWire.exceptions import TransactionCanceledByContract, ContractError
from RapidWire.structs import ChainContext

class TestRapidWireVM(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.api = AsyncMock()
        self.chain_context = ChainContext(total_cost=0, budget=100)
        self.api.chain_context = self.chain_context
        self.api.add_cost = MagicMock()

        def add_cost_side_effect(op):
            self.chain_context.total_cost += 1
            if self.chain_context.total_cost > self.chain_context.budget:
                raise ContractError('Execution budget exceeded.')
        self.api.add_cost.side_effect = add_cost_side_effect
        self.system_vars = {'_sender': 100, '_self': 200, '_input': 'test_input'}

    async def test_arithmetic(self):
        script = [{'op': 'add', 'args': [{'t': 'int', 'v': 10}, {'t': 'int', 'v': 20}], 'out': 'res'}, {'op': 'sub', 'args': [{'t': 'var', 'v': 'res'}, {'t': 'int', 'v': 5}], 'out': 'res2'}]
        vm = RapidWireVM(script, self.api, self.system_vars)
        await vm.run()
        self.assertEqual(vm.vars['res'], 30)
        self.assertEqual(vm.vars['res2'], 25)

    async def test_arithmetic_cast(self):
        script = [{'op': 'add', 'args': [{'t': 'int', 'v': 10}, {'t': 'int', 'v': 20}], 'out': 'res'}, {'op': 'store_set', 'args': [{'t': 'str', 'v': 'temp'}, {'t': 'var', 'v': 'res'}]}, {'op': 'store_get', 'args': [{'t': 'str', 'v': 'temp'}], 'out': '_val'}, {'op': 'add', 'args': [{'t': 'var', 'v': '_val'}, {'t': 'int', 'v': 5}], 'out': '_final'}]

        async def side_effect(user, key):
            if key == 'temp':
                return '30'
            return None
        self.api.get_variable.side_effect = side_effect
        vm = RapidWireVM(script, self.api, self.system_vars)
        await vm.run()
        self.assertEqual(vm.vars['_final'], 35)

    async def test_getitem(self):
        script = [{'op': 'getitem', 'args': [{'t': 'var', 'v': '_my_list'}, {'t': 'int', 'v': 1}], 'out': 'res1'}, {'op': 'getitem', 'args': [{'t': 'var', 'v': '_my_dict'}, {'t': 'str', 'v': 'key'}], 'out': 'res2'}]
        self.system_vars['_my_list'] = ['a', 'b', 'c']
        self.system_vars['_my_dict'] = {'key': 'value'}
        vm = RapidWireVM(script, self.api, self.system_vars)
        await vm.run()
        self.assertEqual(vm.vars['res1'], 'b')
        self.assertEqual(vm.vars['res2'], 'value')

    async def test_flow_control(self):
        script = [{'op': 'eq', 'args': [{'t': 'var', 'v': '_input'}, {'t': 'str', 'v': 'test_input'}], 'out': '_is_match'}, {'op': 'if', 'args': [{'t': 'var', 'v': '_is_match'}], 'then': [{'op': 'output', 'args': [{'t': 'str', 'v': 'Matched'}]}], 'else': [{'op': 'output', 'args': [{'t': 'str', 'v': 'Not Matched'}]}]}]
        vm = RapidWireVM(script, self.api, self.system_vars)
        await vm.run()
        self.assertEqual(vm.output, 'Matched')

    async def test_transfer(self):
        script = [{'op': 'transfer', 'args': [{'t': 'int', 'v': 300}, {'t': 'int', 'v': 10}, {'t': 'int', 'v': 1}]}]
        vm = RapidWireVM(script, self.api, self.system_vars)
        await vm.run()
        self.api.transfer.assert_called_with(200, 300, 1, 10)

    async def test_cancel(self):
        script = [{'op': 'cancel', 'args': [{'t': 'str', 'v': 'Error'}]}]
        vm = RapidWireVM(script, self.api, self.system_vars)
        with self.assertRaises(TransactionCanceledByContract):
            await vm.run()

    async def test_stop_execution(self):
        script = [{'op': 'exit'}, {'op': 'output', 'args': [{'t': 'str', 'v': 'Should not run'}]}]
        vm = RapidWireVM(script, self.api, self.system_vars)
        await vm.run()
        self.assertIsNone(vm.output)

    async def test_store_ops_str(self):
        script = [{'op': 'store_set', 'args': [{'t': 'str', 'v': 'key'}, {'t': 'str', 'v': 'value'}]}, {'op': 'store_get', 'args': [{'t': 'str', 'v': 'key'}], 'out': '_val'}]
        self.api.get_variable.return_value = 'value'
        vm = RapidWireVM(script, self.api, self.system_vars)
        await vm.run()
        self.api.set_variable.assert_called_with('key', 'value')
        self.assertEqual(vm.vars['_val'], 'value')

    async def test_store_ops_int(self):
        script = [{'op': 'store_set', 'args': [{'t': 'str', 'v': 'key'}, {'t': 'int', 'v': 123}]}, {'op': 'store_get', 'args': [{'t': 'str', 'v': 'key'}], 'out': '_val'}]
        self.api.get_variable.return_value = 123
        vm = RapidWireVM(script, self.api, self.system_vars)
        await vm.run()
        self.api.set_variable.assert_called_with('key', '123')
        self.assertEqual(vm.vars['_val'], '123')

    async def test_sha256(self):
        script = [{'op': 'sha256', 'args': [{'t': 'str', 'v': 'hello'}], 'out': 'res'}]
        vm = RapidWireVM(script, self.api, self.system_vars)
        await vm.run()
        self.assertEqual(vm.vars['res'], '2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824')

    async def test_random(self):
        script = [{'op': 'random', 'args': [{'t': 'int', 'v': 1}, {'t': 'int', 'v': 10}], 'out': 'res'}]
        vm = RapidWireVM(script, self.api, self.system_vars)
        await vm.run()
        self.assertTrue(1 <= vm.vars['res'] <= 10)

    async def test_get_allowance(self):
        script = [{'op': 'get_allowance', 'args': [{'t': 'int', 'v': 100}, {'t': 'int', 'v': 200}, {'t': 'int', 'v': 1}], 'out': 'res'}]
        self.api.get_allowance.return_value = 500
        vm = RapidWireVM(script, self.api, self.system_vars)
        await vm.run()
        self.api.get_allowance.assert_called_with(100, 200, 1)
        self.assertEqual(vm.vars['res'], 500)

    async def test_split(self):
        script = [{'op': 'split', 'args': [{'t': 'str', 'v': 'a,b,c'}, {'t': 'str', 'v': ','}], 'out': 'res'}, {'op': 'getitem', 'args': [{'t': 'var', 'v': 'res'}, {'t': 'int', 'v': 1}], 'out': '_val'}]
        vm = RapidWireVM(script, self.api, self.system_vars)
        await vm.run()
        self.assertEqual(vm.vars['res'], ['a', 'b', 'c'])
        self.assertEqual(vm.vars['_val'], 'b')

    async def test_split_default(self):
        script = [{'op': 'split', 'args': [{'t': 'str', 'v': 'hello world'}, {'t': 'str', 'v': ' '}], 'out': 'res'}]
        vm = RapidWireVM(script, self.api, self.system_vars)
        await vm.run()
        self.assertEqual(vm.vars['res'], ['hello', 'world'])

    async def test_to_str(self):
        script = [{'op': 'to_str', 'args': [{'t': 'int', 'v': 123}], 'out': 'res'}]
        vm = RapidWireVM(script, self.api, self.system_vars)
        await vm.run()
        self.assertEqual(vm.vars['res'], '123')
        self.assertIsInstance(vm.vars['res'], str)

    async def test_to_int(self):
        script = [{'op': 'to_int', 'args': [{'t': 'int', 'v': 456}], 'out': 'res'}]
        vm = RapidWireVM(script, self.api, self.system_vars)
        await vm.run()
        self.assertEqual(vm.vars['res'], 456)
        self.assertIsInstance(vm.vars['res'], int)

    async def test_now(self):
        script = [{'op': 'now', 'out': 'res'}]
        vm = RapidWireVM(script, self.api, self.system_vars)
        start_time = int(time.time())
        await vm.run()
        end_time = int(time.time())
        self.assertIsInstance(vm.vars['res'], int)
        self.assertTrue(start_time <= vm.vars['res'] <= end_time)

    async def test_while_loop(self):
        script = [{'op': 'set', 'args': [{'t': 'int', 'v': 0}], 'out': '_i'}, {'op': 'gt', 'args': [{'t': 'int', 'v': 5}, {'t': 'var', 'v': '_i'}], 'out': '_cond'}, {'op': 'while', 'args': [{'t': 'var', 'v': '_cond'}], 'body': [{'op': 'add', 'args': [{'t': 'var', 'v': '_i'}, {'t': 'int', 'v': 1}], 'out': '_i'}, {'op': 'gt', 'args': [{'t': 'int', 'v': 5}, {'t': 'var', 'v': '_i'}], 'out': '_cond'}]}]
        vm = RapidWireVM(script, self.api, self.system_vars)
        await vm.run()
        self.assertEqual(vm.vars['_i'], 5)
        self.assertGreater(self.chain_context.total_cost, 10)
        self.assertLess(self.chain_context.total_cost, 30)

    async def test_infinite_loop_budget_exceeded(self):
        script = [{'op': 'set', 'args': [{'t': 'int', 'v': 1}], 'out': '_cond'}, {'op': 'while', 'args': [{'t': 'var', 'v': '_cond'}], 'body': [{'op': 'add', 'args': [{'t': 'int', 'v': 1}, {'t': 'int', 'v': 1}], 'out': '_dump'}]}]
        self.chain_context.budget = 10
        vm = RapidWireVM(script, self.api, self.system_vars)
        with self.assertRaises(ContractError) as cm:
            await vm.run()
        self.assertIn('Execution budget exceeded', str(cm.exception))
if __name__ == '__main__':
    unittest.main()
