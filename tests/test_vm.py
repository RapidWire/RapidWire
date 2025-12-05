import unittest
from unittest.mock import MagicMock
from RapidWire.vm import RapidWireVM, StopExecution
from RapidWire.exceptions import TransactionCanceledByContract

class TestRapidWireVM(unittest.TestCase):
    def setUp(self):
        self.api = MagicMock()
        self.system_vars = {
            '_tx_source': 100,
            '_tx_dest': 200,
            '_tx_currency': 1,
            '_tx_amount': 50,
            '_tx_input': "test_input"
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

    def test_flow_control(self):
        script = [
            {"op": "eq", "args": ["_tx_input", "test_input"], "out": "_is_match"},
            {
                "op": "if",
                "args": ["_is_match"],
                "then": [
                    {"op": "reply", "args": ["Matched"]}
                ],
                "else": [
                    {"op": "reply", "args": ["Not Matched"]}
                ]
            }
        ]
        vm = RapidWireVM(script, self.api, self.system_vars)
        vm.run()
        self.assertEqual(vm.return_message, "Matched")

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
            {"op": "reply", "args": ["Should not run"]}
        ]
        vm = RapidWireVM(script, self.api, self.system_vars)
        vm.run()
        self.assertIsNone(vm.return_message)

    def test_store_ops(self):
        script = [
            {"op": "store_set", "args": ["key", "value"]},
            {"op": "store_get", "args": ["key"], "out": "_val"}
        ]
        self.api.get_variable.return_value = b'value'
        vm = RapidWireVM(script, self.api, self.system_vars)
        vm.run()
        self.api.set_variable.assert_called_with(b'key', b'value')
        self.assertEqual(vm.vars['_val'], 'value')

if __name__ == '__main__':
    unittest.main()
