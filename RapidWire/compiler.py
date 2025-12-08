import ast
import json
import sys

class Compiler:
    def __init__(self):
        self.temp_counter = 0
        self.instructions = []
        # Mapping for system variables
        self.var_map = {
            'sender': '_sender',
            'self_id': '_self',
            'input_data': '_input',  # Inferred from example output
        }

    def _get_temp_var(self):
        self.temp_counter += 1
        return f"_t{self.temp_counter}"

    def _map_var(self, name):
        if name in self.var_map:
            return self.var_map[name]
        return f"_{name}"

    def compile(self, source_code):
        tree = ast.parse(source_code)
        # Find the main function
        main_node = None
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == 'main':
                main_node = node
                break

        if not main_node:
            raise ValueError("No main function found")

        return self._process_block(main_node.body)

    def _process_block(self, stmts):
        instructions = []
        for stmt in stmts:
            instructions.extend(self._process_stmt(stmt))
        return instructions

    def _process_stmt(self, stmt):
        instrs = []
        if isinstance(stmt, ast.If):
            # Condition
            cond_var, cond_instrs = self._process_expr(stmt.test)
            instrs.extend(cond_instrs)

            # Then block
            then_instrs = self._process_block(stmt.body)

            # Else block
            else_instrs = self._process_block(stmt.orelse)

            if_op = {
                "op": "if",
                "args": [cond_var],
                "then": then_instrs,
                "else": else_instrs
            }
            instrs.append(if_op)

        elif isinstance(stmt, ast.Assign):
            # Handle assignment
            # target = value
            if len(stmt.targets) != 1:
                raise ValueError("Multiple assignment not supported")

            target = stmt.targets[0]
            value_node = stmt.value

            if isinstance(target, ast.Name):
                # Variable assignment: x = ...
                target_name = self._map_var(target.id)
                # If the value is a complex expression, we might need to tell _process_expr to use this target name?
                # However, our design for _process_expr creates temp vars or returns existing ones.
                # If we get a temp var, we assume it holds the value.
                # Since the VM seems to use implicit assignment via 'out',
                # strictly speaking, "x = a + b" -> "add _a _b out=_t1" is fine, but we lose "x".
                # But wait, Rule A says "my_var" -> "_my_var".
                # If we have "my_var = a + b", ideally we want "add _a _b out=_my_var".
                # Or we do "add ... out=_t1" then "set _my_var _t1" (if set exists).
                # The requirements don't mention a 'set' or 'assign' opcode for variables.
                # Instead, Rule A implies variables exist.
                # Rule B says "out": "_t1".
                # Rule E says "x = storage..." -> "out": "_x".
                # This implies we can pass the desired output variable name to _process_expr.

                res_var, res_instrs = self._process_expr(value_node, target_var=target_name)
                instrs.extend(res_instrs)

                # If the result didn't naturally land in target_name (e.g. it was a literal or simple var read),
                # we technically need to assign it. But without a 'set' opcode, maybe we assume
                # we don't handle simple "x = y" or "x = 1" unless it's part of an operation?
                # Let's look at the example: `bal = get_balance(...)`. `get_balance` is a call.
                # `x = storage_str[...]`.
                # Neither is `x = 1` or `x = y`.
                # If we encounter `x = 1`, and no op to set x...
                # Let's assume for now we pass `target_var` to operations that support `out`.

                # If res_var is not target_name (e.g. strict literal assignment or variable copy),
                # AND we have no MOVE instruction, this is tricky.
                # But the prompt examples all show operations with "out".
                # Let's assume we use `out=target_name` when possible.
                pass

            elif isinstance(target, ast.Subscript):
                # Storage assignment: storage_int["key"] = x
                # Target is storage_int["key"]
                if isinstance(target.value, ast.Name):
                    storage_type = target.value.id # storage_str or storage_int

                    # Key handling
                    key = None
                    if isinstance(target.slice, ast.Constant): # python 3.9+
                        key = target.slice.value
                    elif isinstance(target.slice, ast.Index): # python < 3.9
                        if isinstance(target.slice.value, ast.Constant): # python 3.4+
                            key = target.slice.value.value
                        elif isinstance(target.slice.value, ast.Str):
                             key = target.slice.value.s

                    if not key:
                        # If key is variable, we might need to evaluate it?
                        # Requirement E says `storage_int["key"]`. It implies literal string key usually.
                        # But `args` in JSON is a list.
                        # Let's handle generic expression for key.
                        k_var, k_instrs = self._process_expr(target.slice) # or target.slice.value depending on python ver
                        # Wait, ast structure for subscript varies.
                        # Let's use a helper for key extraction.
                        pass

                    # Key evaluation (if not handling complex keys, just take literal)
                    # The example shows "args": ["key"].

                    # For assignment, we evaluate the RHS value.
                    val_var, val_instrs = self._process_expr(value_node)
                    instrs.extend(val_instrs)

                    op_name = None
                    if storage_type == 'storage_int':
                        op_name = 'store_int_set'
                    elif storage_type == 'storage_str':
                        op_name = 'store_str_set'
                    else:
                         # Default fallback or error?
                         # Maybe generic storage assignment?
                         op_name = 'store_set'

                    # Extract key value if it's a constant
                    key_arg = self._extract_key(target)

                    instrs.append({
                        "op": op_name,
                        "args": [key_arg, val_var]
                    })

        elif isinstance(stmt, ast.Expr):
            # Expression statement (e.g. function call without assignment)
            _, expr_instrs = self._process_expr(stmt.value)
            instrs.extend(expr_instrs)

        return instrs

    def _extract_key(self, subscript_node):
        # Extracts key from subscript. Currently assumes string literal as per example.
        # But if it's a variable, we should probably handle that.
        # However, requirements Example E: `storage_str["key"]` -> args ["key"] (string literal).
        # It doesn't use `_key_var`.
        # I'll stick to extracting the string value.
        slice_node = subscript_node.slice
        if isinstance(slice_node, ast.Constant):
            return slice_node.value
        # Python < 3.9 compat if needed (not needed for modern environments usually, but safe to check)
        return str(slice_node) # Fallback

    def _process_expr(self, node, target_var=None):
        # Returns (result_variable_name, list_of_instructions)
        instrs = []

        if isinstance(node, ast.Constant):
            # Literal
            val = node.value
            return str(val), [] # Return the value as string, no instructions

        elif isinstance(node, ast.Name):
            # Variable reference
            return self._map_var(node.id), []

        elif isinstance(node, ast.BinOp):
            # Binary Operation
            left_var, left_instrs = self._process_expr(node.left)
            right_var, right_instrs = self._process_expr(node.right)
            instrs.extend(left_instrs)
            instrs.extend(right_instrs)

            op_map = {
                ast.Add: 'add',
                ast.Sub: 'sub',
                ast.Mult: 'mul',
                ast.Div: 'div', # // is FloorDiv in Python, / is Div.
                ast.FloorDiv: 'div', # Assume // maps to div for integer VM
                ast.Mod: 'mod'
            }

            op_name = op_map.get(type(node.op))
            if not op_name:
                raise ValueError(f"Unsupported operator {node.op}")

            out_var = target_var if target_var else self._get_temp_var()
            instrs.append({
                "op": op_name,
                "args": [left_var, right_var],
                "out": out_var
            })
            return out_var, instrs

        elif isinstance(node, ast.Compare):
            # Comparison
            # Assume simple comparison a == b
            if len(node.ops) != 1 or len(node.comparators) != 1:
                raise ValueError("Only single comparison supported")

            left_var, left_instrs = self._process_expr(node.left)
            right_var, right_instrs = self._process_expr(node.comparators[0])
            instrs.extend(left_instrs)
            instrs.extend(right_instrs)

            op_map = {
                ast.Eq: 'eq',
                ast.Gt: 'gt',
                ast.Lt: 'lt',
                # Add others if needed
            }
            op_name = op_map.get(type(node.ops[0]))

            out_var = target_var if target_var else self._get_temp_var()
            instrs.append({
                "op": op_name,
                "args": [left_var, right_var],
                "out": out_var
            })
            return out_var, instrs

        elif isinstance(node, ast.Call):
            # Function call
            func_name = node.func.id
            args = []
            for arg in node.args:
                arg_var, arg_instrs = self._process_expr(arg)
                instrs.extend(arg_instrs)
                args.append(arg_var)

            # Mapping function names to ops
            # Rule C
            call_map = {
                'reply': 'reply',
                'transfer': 'transfer',
                'sha256': 'hash',
                'random': 'random',
                'get_balance': 'get_balance',
                'cancel': 'cancel',
                'exit': 'exit'
            }

            op_name = call_map.get(func_name, func_name)

            # Ops that produce output
            # sha256 -> out, random -> out, get_balance -> out
            ops_with_out = ['hash', 'random', 'get_balance']

            op_obj = {
                "op": op_name,
                "args": args
            }

            res_var = None
            if op_name in ops_with_out:
                res_var = target_var if target_var else self._get_temp_var()
                op_obj["out"] = res_var

            instrs.append(op_obj)
            return res_var, instrs

        elif isinstance(node, ast.Subscript):
            # Storage Get: x = storage_str["key"]
            # Handled here because it's an expression on the RHS
            if isinstance(node.value, ast.Name):
                storage_type = node.value.id

                # Extract key
                key_arg = self._extract_key(node)

                op_name = None
                if storage_type == 'storage_str':
                    # Rule E: storage_str["key"] -> store_str_get
                    op_name = 'store_str_get'
                elif storage_type == 'storage_int':
                    op_name = 'store_int_get'
                else:
                    op_name = 'store_get' # Fallback

                out_var = target_var if target_var else self._get_temp_var()
                instrs.append({
                    "op": op_name,
                    "args": [key_arg],
                    "out": out_var
                })
                return out_var, instrs

        else:
             raise ValueError(f"Unsupported expression type: {type(node)}")

def main():
    if len(sys.argv) > 1:
        with open(sys.argv[1], 'r') as f:
            code = f.read()
    else:
        # Read from stdin
        code = sys.stdin.read()

    compiler = Compiler()
    try:
        result = compiler.compile(code)
        print(json.dumps(result, indent=2))
    except Exception as e:
        sys.stderr.write(f"Error: {e}\n")
        sys.exit(1)

if __name__ == '__main__':
    main()
