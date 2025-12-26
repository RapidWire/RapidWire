import unittest
from unittest.mock import MagicMock
from RapidWire.vm import RapidWireVM, StopExecution
from RapidWire.exceptions import TransactionCanceledByContract

class TestRapidWireVM(unittest.TestCase):
    def setUp(self):
        self.api = MagicMock()
        self.system_vars = {
            '_sender': 100,
            '_self': 200,
            '_input': "test_input"
        }

    def test_arithmetic(self):
        script = [
            {"op": "add", "args": ["10", "20"], "out": "_res"},
            {"op": "sub", "args": ["_res", "5"], "out": "_res2"},
        ]
        vm = RapidWireVM(script, self.api, self.system_vars)
        vm.run()
        self.assertEqual(vm.vars['_res'], 30)
        self.assertEqual(vm.vars['_res2'], 25)

    def test_arithmetic_cast(self):
        script = [
            {"op": "add", "args": ["10", "20"], "out": "_res"},
            {"op": "store_int_set", "args": ["temp", "_res"]}, # Store 30
            {"op": "store_int_get", "args": ["temp"], "out": "_val"}, # Get 30
            {"op": "add", "args": ["_val", "5"], "out": "_final"} # Should be 30 + 5 = 35
        ]
        # Mock api response for store_get
        def side_effect(user, key):
            if key == 'temp':
                return '30'
            return None
        self.api.get_variable.side_effect = side_effect

        vm = RapidWireVM(script, self.api, self.system_vars)
        vm.run()
        self.assertEqual(vm.vars['_final'], 35)

    def test_getitem(self):
        script = [
            {"op": "getitem", "args": ["_my_list", "1"], "out": "_res1"},
            {"op": "getitem", "args": ["_my_dict", "key"], "out": "_res2"}
        ]
        self.system_vars['_my_list'] = ["a", "b", "c"]
        self.system_vars['_my_dict'] = {"key": "value"}

        vm = RapidWireVM(script, self.api, self.system_vars)
        vm.run()
        self.assertEqual(vm.vars['_res1'], "b")
        self.assertEqual(vm.vars['_res2'], "value")

    def test_flow_control(self):
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
        vm.run()
        self.assertEqual(vm.output, "Matched")

    def test_transfer(self):
        script = [
            {"op": "transfer", "args": ["300", "10", "1"]}
        ]
        vm = RapidWireVM(script, self.api, self.system_vars)
        vm.run()
        self.api.transfer.assert_called_with(200, 300, 1, 10) # 200 is source (self), 300 dest, 1 cur, 10 amount

    def test_cancel(self):
        script = [
            {"op": "cancel", "args": ["Error"]}
        ]
        vm = RapidWireVM(script, self.api, self.system_vars)
        with self.assertRaises(TransactionCanceledByContract):
            vm.run()

    def test_stop_execution(self):
        script = [
            {"op": "exit"},
            {"op": "output", "args": ["Should not run"]}
        ]
        vm = RapidWireVM(script, self.api, self.system_vars)
        vm.run()
        self.assertIsNone(vm.output)

    def test_store_ops_str(self):
        script = [
            {"op": "store_str_set", "args": ["key", "value"]},
            {"op": "store_str_get", "args": ["key"], "out": "_val"}
        ]
        self.api.get_variable.return_value = 'value'
        vm = RapidWireVM(script, self.api, self.system_vars)
        vm.run()
        self.api.set_variable.assert_called_with('key', 'value')
        self.assertEqual(vm.vars['_val'], 'value')

    def test_store_ops_int(self):
        script = [
            {"op": "store_int_set", "args": ["key", 123]},
            {"op": "store_int_get", "args": ["key"], "out": "_val"}
        ]
        self.api.get_variable.return_value = 123
        vm = RapidWireVM(script, self.api, self.system_vars)
        vm.run()
        self.api.set_variable.assert_called_with('key', 123)
        self.assertEqual(vm.vars['_val'], 123)

    def test_store_ops_type_safety(self):
        # test that store_str_get returns string even if stored value is something else (or None)
        script = [
            {"op": "store_str_get", "args": ["key"], "out": "_val"}
        ]
        self.api.get_variable.return_value = 123
        vm = RapidWireVM(script, self.api, self.system_vars)
        vm.run()
        self.assertEqual(vm.vars['_val'], "123")
        self.assertIsInstance(vm.vars['_val'], str)

        # test that store_int_get returns int even if stored value is string "123"
        script = [
            {"op": "store_int_get", "args": ["key"], "out": "_val"}
        ]
        self.api.get_variable.return_value = "123"
        vm = RapidWireVM(script, self.api, self.system_vars)
        vm.run()
        self.assertEqual(vm.vars['_val'], 123)
        self.assertIsInstance(vm.vars['_val'], int)

    def test_sha256(self):
        script = [
            {"op": "sha256", "args": ["hello"], "out": "_res"}
        ]
        vm = RapidWireVM(script, self.api, self.system_vars)
        vm.run()
        # "hello" sha256: 2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824
        self.assertEqual(vm.vars['_res'], "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824")

    def test_random(self):
        script = [
            {"op": "random", "args": ["1", "10"], "out": "_res"}
        ]
        vm = RapidWireVM(script, self.api, self.system_vars)
        vm.run()
        self.assertTrue(1 <= vm.vars['_res'] <= 10)

    def test_get_allowance(self):
        script = [
            {"op": "get_allowance", "args": ["100", "200", "1"], "out": "_res"}
        ]
        self.api.get_allowance.return_value = 500
        vm = RapidWireVM(script, self.api, self.system_vars)
        vm.run()
        self.api.get_allowance.assert_called_with(100, 200, 1)
        self.assertEqual(vm.vars['_res'], 500)

if __name__ == '__main__':
    unittest.main()
