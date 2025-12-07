import unittest
from unittest.mock import MagicMock
from RapidWire.vm import RapidWireVM

class TestTypedVariables(unittest.TestCase):
    def setUp(self):
        self.api = MagicMock()
        self.system_vars = {
            '_sender': 100,
            '_self': 200,
            '_input': "test_input"
        }

    def test_store_int(self):
        script = [
            {"op": "store_set", "args": ["count", 42]},
            {"op": "store_get", "args": ["count"], "out": "_val"}
        ]

        # Mock api response
        def get_side_effect(user, key):
            if key == b'count':
                return 42
            return None
        self.api.get_variable.side_effect = get_side_effect

        vm = RapidWireVM(script, self.api, self.system_vars)
        vm.run()

        self.api.set_variable.assert_called_with(b'count', 42)
        self.assertEqual(vm.vars['_val'], 42)
        self.assertIsInstance(vm.vars['_val'], int)

    def test_store_str(self):
        script = [
            {"op": "store_set", "args": ["name", "Alice"]},
            {"op": "store_get", "args": ["name"], "out": "_val"}
        ]

        # Mock api response
        def get_side_effect(user, key):
            if key == b'name':
                return "Alice"
            return None
        self.api.get_variable.side_effect = get_side_effect

        vm = RapidWireVM(script, self.api, self.system_vars)
        vm.run()

        self.api.set_variable.assert_called_with(b'name', "Alice")
        self.assertEqual(vm.vars['_val'], "Alice")
        self.assertIsInstance(vm.vars['_val'], str)

    def test_type_switching(self):
        # Test that vm passes correct types to api even if variable reuse happens
        # Though API/Model logic handles the DB switching, VM just passes what it gets.
        script = [
            {"op": "store_set", "args": ["var", 100]},
            {"op": "store_set", "args": ["var", "text"]}
        ]

        vm = RapidWireVM(script, self.api, self.system_vars)
        vm.run()

        calls = self.api.set_variable.call_args_list
        self.assertEqual(calls[0].args, (b'var', 100))
        self.assertEqual(calls[1].args, (b'var', "text"))

if __name__ == '__main__':
    unittest.main()
