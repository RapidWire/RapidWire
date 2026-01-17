import ast
import json
import sys
import argparse

class Compiler:
    def __init__(self):
        self.temp_counter = 0
        self.instructions = []
        # Mapping for system variables
        self.var_map = {
            'sender': '_sender',
            'self_id': '_self',
            'input_data': '_input',
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

        elif isinstance(stmt, ast.While):
            # While loop

            # If condition is an expression (e.g. i < 10), we need to:
            # 1. Evaluate it once before loop.
            # 2. Re-evaluate it at the end of loop body.

            cond_var, cond_instrs = self._process_expr(stmt.test)

            # Initial evaluation instructions
            instrs.extend(cond_instrs)

            # Body instructions
            body_instrs = self._process_block(stmt.body)

            # Re-evaluate condition at the end of body
            # We need to reuse the same output variable 'cond_var' so the while loop checks it.
            # Rerun the expression processing, but targeting cond_var.
            _, recheck_instrs = self._process_expr(stmt.test, target_var=cond_var)
            body_instrs.extend(recheck_instrs)

            while_op = {
                "op": "while",
                "args": [cond_var],
                "body": body_instrs
            }
            instrs.append(while_op)

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
                res_var, res_instrs = self._process_expr(value_node, target_var=target_name)
                instrs.extend(res_instrs)

                # If the result of expression is just a variable name (no instruction generated targeting target_name),
                # we need to add an explicit assignment instruction.
                if res_var != target_name:
                     instrs.append({
                        "op": "set",
                        "args": [res_var],
                        "out": target_name
                     })

            elif isinstance(target, ast.Subscript):
                # Storage assignment: storage["key"] = x
                if isinstance(target.value, ast.Name):
                    storage_type = target.value.id

                    # Check if it is storage assignment
                    if storage_type == 'storage':
                        # For assignment, we evaluate the RHS value.
                        val_var, val_instrs = self._process_expr(value_node)
                        instrs.extend(val_instrs)

                        # Extract key value
                        key_var, key_instrs = self._extract_key(target)
                        instrs.extend(key_instrs)

                        instrs.append({
                            "op": 'store_set',
                            "args": [key_var, val_var]
                        })
                    else:
                        raise ValueError(f"Unsupported assignment target: {storage_type}")
                else:
                     raise ValueError("Unsupported assignment target structure")

        elif isinstance(stmt, ast.Expr):
            # Expression statement
            _, expr_instrs = self._process_expr(stmt.value)
            instrs.extend(expr_instrs)

        return instrs

    def _extract_key(self, subscript_node):
        slice_node = subscript_node.slice

        # Python < 3.9
        if hasattr(ast, 'Index') and isinstance(slice_node, ast.Index):
            slice_node = slice_node.value

        return self._process_expr(slice_node)

    def _process_expr(self, node, target_var=None):
        # Returns (result_variable_name, list_of_instructions)
        instrs = []

        if isinstance(node, ast.Constant):
            # Literal
            val = node.value
            return str(val), []

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
                ast.Div: 'div',
                ast.FloorDiv: 'div',
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
            if len(node.ops) != 1 or len(node.comparators) != 1:
                raise ValueError("Only single comparison supported")

            left_var, left_instrs = self._process_expr(node.left)
            right_var, right_instrs = self._process_expr(node.comparators[0])
            instrs.extend(left_instrs)
            instrs.extend(right_instrs)

            op_map = {
                ast.Eq: 'eq',
                ast.NotEq: 'neq',
                ast.Gt: 'gt',
                ast.Lt: 'lt',
                ast.LtE: 'lte',
                ast.GtE: 'gte',
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

            func_map = {
                'str': 'to_str',
                'int': 'to_int',
                'len': 'length'
            }
            op_name = func_map.get(func_name, func_name)

            # Ops that produce output
            # Note: discord_send/role_add return int (success), transfer_from returns result.
            ops_with_out = [
                'sha256', 'random', 'get_balance', 'concat',
                'transfer_from', 'get_allowance', 'get_currency', 'get_transaction',
                'create_claim', 'pay_claim', 'cancel_claim', 'execute',
                'discord_send', 'discord_role_add', 'has_role', 'length',
                'split', 'to_str', 'to_int', 'now',
            ]

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

        elif isinstance(node, ast.Attribute):
            # Attribute access: obj.prop -> attr(obj, prop)
            obj_var, obj_instrs = self._process_expr(node.value)
            instrs.extend(obj_instrs)

            prop_name = node.attr

            out_var = target_var if target_var else self._get_temp_var()
            instrs.append({
                "op": "attr",
                "args": [obj_var, prop_name],
                "out": out_var
            })
            return out_var, instrs

        elif isinstance(node, ast.Subscript):
            # Subscript access: obj[key]
            # First check if it's storage access (special case)
            is_storage = False
            if isinstance(node.value, ast.Name):
                if node.value.id == 'storage':
                    is_storage = True

            if is_storage:
                key_var, key_instrs = self._extract_key(node)
                instrs.extend(key_instrs)

                out_var = target_var if target_var else self._get_temp_var()
                instrs.append({
                    "op": 'store_get',
                    "args": [key_var],
                    "out": out_var
                })
                return out_var, instrs
            else:
                # Generic getitem, attr access, or slice
                obj_var, obj_instrs = self._process_expr(node.value)
                instrs.extend(obj_instrs)

                # Check if this is a Slice
                slice_node = node.slice
                # Python < 3.9: Subscript(slice=Index(value=...)) for index
                # Python < 3.9: Subscript(slice=Slice(...)) for slice (NO Index wrapper)
                # Python >= 3.9: Subscript(slice=...)

                is_slice = False
                if isinstance(slice_node, ast.Slice):
                    is_slice = True

                if is_slice:
                    # Handle Slicing: [lower:upper:step]
                    # Each part can be None (if omitted) or an expression.

                    def process_slice_part(part):
                        if part is None:
                            return None, []
                        return self._process_expr(part)

                    lower_var, lower_instrs = process_slice_part(slice_node.lower)
                    instrs.extend(lower_instrs)

                    upper_var, upper_instrs = process_slice_part(slice_node.upper)
                    instrs.extend(upper_instrs)

                    step_var, step_instrs = process_slice_part(slice_node.step)
                    instrs.extend(step_instrs)

                    out_var = target_var if target_var else self._get_temp_var()
                    instrs.append({
                        "op": "slice",
                        "args": [obj_var, lower_var, upper_var, step_var],
                        "out": out_var
                    })
                    return out_var, instrs

                else:
                    # Normal Index/Key Access
                    idx_node = slice_node
                    if isinstance(idx_node, ast.Index):
                        idx_node = idx_node.value

                    # Check if index is a string constant (Attribute access)
                    is_string_const = False
                    if isinstance(idx_node, ast.Constant) and isinstance(idx_node.value, str):
                        is_string_const = True
                    elif isinstance(idx_node, ast.Str): # Python < 3.8
                        is_string_const = True

                    if is_string_const:
                        # Treat as attribute access: obj['key'] -> attr(obj, 'key')
                        prop_name = idx_node.value if isinstance(idx_node, ast.Constant) else idx_node.s

                        out_var = target_var if target_var else self._get_temp_var()
                        instrs.append({
                            "op": "attr",
                            "args": [obj_var, prop_name],
                            "out": out_var
                        })
                        return out_var, instrs

                    # For generic getitem, index can be expression
                    idx_var, idx_instrs = self._process_expr(idx_node)
                    instrs.extend(idx_instrs)

                    out_var = target_var if target_var else self._get_temp_var()
                    instrs.append({
                        "op": "getitem",
                        "args": [obj_var, idx_var],
                        "out": out_var
                    })
                    return out_var, instrs

        elif isinstance(node, ast.BoolOp):
            # Boolean Operation (and, or)
            op_type = type(node.op)
            values = node.values
            out_var = target_var if target_var else self._get_temp_var()

            def process_bool_op(values, target):
                # Helper to recursively process bool ops
                if len(values) == 1:
                    v_var, v_instrs = self._process_expr(values[0], target_var=target)
                    # If process_expr didn't assign to target (e.g. constant/variable), generate set
                    if v_var != target:
                        v_instrs.append({"op": "set", "args": [v_var], "out": target})
                    return v_instrs

                head = values[0]
                head_var, head_instrs = self._process_expr(head)

                # Assign head result to target initially
                if head_var != target:
                    head_instrs.append({"op": "set", "args": [head_var], "out": target})

                # Recursively process the rest
                rest_instrs = process_bool_op(values[1:], target)

                if op_type == ast.And:
                    # if target (head) is true, evaluate rest. Else target remains head (false).
                    if_op = {
                        "op": "if",
                        "args": [target],
                        "then": rest_instrs,
                        "else": []
                    }
                    head_instrs.append(if_op)
                elif op_type == ast.Or:
                    # if target (head) is true, keep it. Else evaluate rest.
                    if_op = {
                        "op": "if",
                        "args": [target],
                        "then": [],
                        "else": rest_instrs
                    }
                    head_instrs.append(if_op)
                else:
                    raise ValueError(f"Unsupported BoolOp: {op_type}")

                return head_instrs

            return out_var, process_bool_op(values, out_var)

        else:
             raise ValueError(f"Unsupported expression type: {type(node)}")

def main():
    parser = argparse.ArgumentParser(description='RapidWire Compiler')
    parser.add_argument('filename', help='Input source file')
    args = parser.parse_args()
    input_filename = str(args.filename)

    try:
        with open(input_filename, 'r', encoding="utf-8") as f:
            code = f.read()
    except FileNotFoundError:
        sys.stderr.write(f"Error: File '{input_filename}' not found.\n")
        sys.exit(1)

    compiler = Compiler()

    try:
        result = compiler.compile(code)
        split_filename = input_filename.split(".")
        base_name = ".".join(split_filename[:-1])
        output_filename = f"{base_name}.json" if len(split_filename) >= 2 else f"{input_filename}.json"
        with open(output_filename, 'w', encoding="utf-8") as f:
            json.dump(result, f, indent=2)
        print(f"{input_filename} -> {output_filename}")
    except Exception as e:
        sys.stderr.write(f"Error: {e}\n")
        sys.exit(1)

if __name__ == '__main__':
    main()
