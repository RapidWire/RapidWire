import unittest
from unittest.mock import MagicMock, AsyncMock
import asyncio

import sys
from pathlib import Path
parent_dir = Path(__file__).resolve().parent.parent
sys.path.append(str(parent_dir))

from RapidWire.vm import RapidWireVM
from RapidWire.structs import ChainContext

class TestRapidWireVMComparisons(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.api = AsyncMock()
        self.chain_context = ChainContext(total_cost=0, budget=100)
        self.api.chain_context = self.chain_context
        self.api.add_cost = MagicMock()

        def add_cost_side_effect(op):
            self.chain_context.total_cost += 1
        self.api.add_cost.side_effect = add_cost_side_effect
        self.system_vars = {}

    async def test_lt(self):
        script = [{'op': 'lt', 'args': [{'t': 'int', 'v': 10}, {'t': 'int', 'v': 20}], 'out': 'res1'}, {'op': 'lt', 'args': [{'t': 'int', 'v': 20}, {'t': 'int', 'v': 10}], 'out': 'res2'}, {'op': 'lt', 'args': [{'t': 'int', 'v': 10}, {'t': 'int', 'v': 10}], 'out': 'res3'}]
        vm = RapidWireVM(script, self.api, self.system_vars)
        await vm.run()
        self.assertEqual(vm.vars['res1'], 1)
        self.assertEqual(vm.vars['res2'], 0)
        self.assertEqual(vm.vars['res3'], 0)

    async def test_neq(self):
        script = [{'op': 'neq', 'args': [{'t': 'str', 'v': 'a'}, {'t': 'str', 'v': 'b'}], 'out': 'res1'}, {'op': 'neq', 'args': [{'t': 'str', 'v': 'a'}, {'t': 'str', 'v': 'a'}], 'out': 'res2'}]
        vm = RapidWireVM(script, self.api, self.system_vars)
        await vm.run()
        self.assertEqual(vm.vars['res1'], 1)
        self.assertEqual(vm.vars['res2'], 0)

    async def test_lte(self):
        script = [{'op': 'lte', 'args': [{'t': 'int', 'v': 10}, {'t': 'int', 'v': 20}], 'out': 'res1'}, {'op': 'lte', 'args': [{'t': 'int', 'v': 20}, {'t': 'int', 'v': 10}], 'out': 'res2'}, {'op': 'lte', 'args': [{'t': 'int', 'v': 10}, {'t': 'int', 'v': 10}], 'out': 'res3'}]
        vm = RapidWireVM(script, self.api, self.system_vars)
        await vm.run()
        self.assertEqual(vm.vars['res1'], 1)
        self.assertEqual(vm.vars['res2'], 0)
        self.assertEqual(vm.vars['res3'], 1)

    async def test_gte(self):
        script = [{'op': 'gte', 'args': [{'t': 'int', 'v': 20}, {'t': 'int', 'v': 10}], 'out': 'res1'}, {'op': 'gte', 'args': [{'t': 'int', 'v': 10}, {'t': 'int', 'v': 20}], 'out': 'res2'}, {'op': 'gte', 'args': [{'t': 'int', 'v': 10}, {'t': 'int', 'v': 10}], 'out': 'res3'}]
        vm = RapidWireVM(script, self.api, self.system_vars)
        await vm.run()
        self.assertEqual(vm.vars['res1'], 1)
        self.assertEqual(vm.vars['res2'], 0)
        self.assertEqual(vm.vars['res3'], 1)
