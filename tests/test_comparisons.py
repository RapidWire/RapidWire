import unittest
import asyncio
from unittest.mock import MagicMock, AsyncMock
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
        script = [
            {"op": "lt", "args": ["10", "20"], "out": "_res1"}, # 1
            {"op": "lt", "args": ["20", "10"], "out": "_res2"}, # 0
            {"op": "lt", "args": ["10", "10"], "out": "_res3"}  # 0
        ]
        vm = RapidWireVM(script, self.api, self.system_vars)
        await vm.run()
        self.assertEqual(vm.vars['_res1'], 1)
        self.assertEqual(vm.vars['_res2'], 0)
        self.assertEqual(vm.vars['_res3'], 0)

    async def test_neq(self):
        script = [
            {"op": "neq", "args": ["a", "b"], "out": "_res1"}, # 1
            {"op": "neq", "args": ["a", "a"], "out": "_res2"}  # 0
        ]
        vm = RapidWireVM(script, self.api, self.system_vars)
        await vm.run()
        self.assertEqual(vm.vars['_res1'], 1)
        self.assertEqual(vm.vars['_res2'], 0)

    async def test_lte(self):
        script = [
            {"op": "lte", "args": ["10", "20"], "out": "_res1"}, # 1
            {"op": "lte", "args": ["20", "10"], "out": "_res2"}, # 0
            {"op": "lte", "args": ["10", "10"], "out": "_res3"}  # 1
        ]
        vm = RapidWireVM(script, self.api, self.system_vars)
        await vm.run()
        self.assertEqual(vm.vars['_res1'], 1)
        self.assertEqual(vm.vars['_res2'], 0)
        self.assertEqual(vm.vars['_res3'], 1)

    async def test_gte(self):
        script = [
            {"op": "gte", "args": ["20", "10"], "out": "_res1"}, # 1
            {"op": "gte", "args": ["10", "20"], "out": "_res2"}, # 0
            {"op": "gte", "args": ["10", "10"], "out": "_res3"}  # 1
        ]
        vm = RapidWireVM(script, self.api, self.system_vars)
        await vm.run()
        self.assertEqual(vm.vars['_res1'], 1)
        self.assertEqual(vm.vars['_res2'], 0)
        self.assertEqual(vm.vars['_res3'], 1)
