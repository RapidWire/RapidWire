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
        return name

    def _create_var_arg(self, name):
        return {"t": "var", "v": name}

    def _create_int_arg(self, val):
        return {"t": "int", "v": val}

    def _create_str_arg(self, val):
        return {"t": "str", "v": val}

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
            cond_arg, cond_instrs = self._process_expr(stmt.test)
            instrs.extend(cond_instrs)

            # Then block
            then_instrs = self._process_block(stmt.body)

            # Else block
            else_instrs = self._process_block(stmt.orelse)

            if_op = {
                "op": "if",
                "args": [cond_arg] if cond_arg else [], # Guard against None? If needs condition, None is bad.
                "then": then_instrs,
                "else": else_instrs
            }
            instrs.append(if_op)

        elif isinstance(stmt, ast.While):
            # While loop

            cond_arg, cond_instrs = self._process_expr(stmt.test)
            instrs.extend(cond_instrs)

            body_instrs = self._process_block(stmt.body)

            cond_var_name = None
            if cond_arg and cond_arg['t'] == 'var':
                cond_var_name = cond_arg['v']

            res_arg, recheck_instrs = self._process_expr(stmt.test, target_var=cond_var_name)
            body_instrs.extend(recheck_instrs)

            if cond_arg and cond_arg['t'] == 'var':
                if res_arg is not None and res_arg != cond_arg:
                    body_instrs.append({
                        "op": "set",
                        "args": [res_arg],
                        "out": cond_arg['v']
                    })

            while_op = {
                "op": "while",
                "args": [cond_arg] if cond_arg else [],
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
                res_arg, res_instrs = self._process_expr(value_node, target_var=target_name)
                instrs.extend(res_instrs)

                if res_arg is not None:
                    # Check if res_arg is exactly the target variable
                    is_same_var = (res_arg['t'] == 'var' and res_arg['v'] == target_name)

                    if not is_same_var:
                        instrs.append({
                            "op": "set",
                            "args": [res_arg],
                            "out": target_name
                        })

            elif isinstance(target, ast.Subscript):
                # Storage assignment: storage["key"] = x
                if isinstance(target.value, ast.Name):
                    storage_type = target.value.id

                    if storage_type == 'storage':
                        val_arg, val_instrs = self._process_expr(value_node)
                        instrs.extend(val_instrs)

                        key_arg, key_instrs = self._extract_key(target)
                        instrs.extend(key_instrs)

                        # Guard val_arg None?
                        if val_arg is None:
                             # Assigning void result to storage?
                             pass
                        else:
                            instrs.append({
                                "op": 'store_set',
                                "args": [key_arg, val_arg]
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
        # Returns (result_arg, list_of_instructions)
        # result_arg is {"t": "...", "v": ...}
        instrs = []

        if isinstance(node, ast.Constant):
            # Literal
            val = node.value
            if isinstance(val, bool):
                return self._create_int_arg(1 if val else 0), []
            if isinstance(val, int):
                return self._create_int_arg(val), []
            if isinstance(val, str):
                return self._create_str_arg(val), []
            return self._create_str_arg(str(val)), []

        elif isinstance(node, ast.Name):
            # Variable reference
            return self._create_var_arg(self._map_var(node.id)), []

        elif isinstance(node, ast.BinOp):
            # Binary Operation
            left_arg, left_instrs = self._process_expr(node.left)
            right_arg, right_instrs = self._process_expr(node.right)
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
                "args": [left_arg, right_arg], # Assume left/right not None
                "out": out_var
            })
            return self._create_var_arg(out_var), instrs

        elif isinstance(node, ast.Compare):
            # Comparison
            if len(node.ops) != 1 or len(node.comparators) != 1:
                raise ValueError("Only single comparison supported")

            left_arg, left_instrs = self._process_expr(node.left)
            right_arg, right_instrs = self._process_expr(node.comparators[0])
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
                "args": [left_arg, right_arg],
                "out": out_var
            })
            return self._create_var_arg(out_var), instrs

        elif isinstance(node, ast.Call):
            # Function call
            func_name = node.func.id
            args = []
            for arg in node.args:
                arg_val, arg_instrs = self._process_expr(arg)
                instrs.extend(arg_instrs)
                # Should we guard against arg_val being None here?
                # If a function is called with a void result as argument, it's weird.
                # But let's assume valid calls.
                args.append(arg_val)

            func_map = {
                'str': 'to_str',
                'int': 'to_int',
                'len': 'length'
            }
            op_name = func_map.get(func_name, func_name)

            # Ops that produce output
            ops_with_out = [
                'sha256', 'random', 'get_balance', 'concat',
                'transfer_from', 'get_allowance', 'get_currency', 'get_transaction',
                'create_claim', 'pay_claim', 'cancel_claim', 'execute',
                'discord_send', 'discord_role_add', 'has_role', 'length',
                'split', 'to_str', 'to_int', 'now',
                'swap', 'add_liquidity', 'remove_liquidity',
                'store_get',
            ]

            op_obj = {
                "op": op_name,
                "args": args
            }

            res_arg = None
            if op_name in ops_with_out:
                out_var = target_var if target_var else self._get_temp_var()
                op_obj["out"] = out_var
                res_arg = self._create_var_arg(out_var)

            instrs.append(op_obj)
            return res_arg, instrs

        elif isinstance(node, ast.Attribute):
            # Attribute access: obj.prop -> attr(obj, prop)
            obj_arg, obj_instrs = self._process_expr(node.value)
            instrs.extend(obj_instrs)

            prop_name = node.attr

            out_var = target_var if target_var else self._get_temp_var()
            instrs.append({
                "op": "attr",
                "args": [obj_arg, self._create_str_arg(prop_name)],
                "out": out_var
            })
            return self._create_var_arg(out_var), instrs

        elif isinstance(node, ast.Subscript):
            # Subscript access: obj[key]
            is_storage = False
            if isinstance(node.value, ast.Name):
                if node.value.id == 'storage':
                    is_storage = True

            if is_storage:
                key_arg, key_instrs = self._extract_key(node)
                instrs.extend(key_instrs)

                out_var = target_var if target_var else self._get_temp_var()
                instrs.append({
                    "op": 'store_get',
                    "args": [key_arg],
                    "out": out_var
                })
                return self._create_var_arg(out_var), instrs
            else:
                # Generic getitem, attr access, or slice
                obj_arg, obj_instrs = self._process_expr(node.value)
                instrs.extend(obj_instrs)

                slice_node = node.slice
                is_slice = False
                if isinstance(slice_node, ast.Slice):
                    is_slice = True

                if is_slice:
                    def process_slice_part(part):
                        if part is None:
                            return None, []
                        return self._process_expr(part)

                    lower_arg, lower_instrs = process_slice_part(slice_node.lower)
                    instrs.extend(lower_instrs)

                    upper_arg, upper_instrs = process_slice_part(slice_node.upper)
                    instrs.extend(upper_instrs)

                    step_arg, step_instrs = process_slice_part(slice_node.step)
                    instrs.extend(step_instrs)

                    out_var = target_var if target_var else self._get_temp_var()

                    args_list = [obj_arg]
                    args_list.append(lower_arg if lower_arg else None)
                    args_list.append(upper_arg if upper_arg else None)
                    args_list.append(step_arg if step_arg else None)

                    instrs.append({
                        "op": "slice",
                        "args": args_list,
                        "out": out_var
                    })
                    return self._create_var_arg(out_var), instrs

                else:
                    idx_node = slice_node
                    if isinstance(idx_node, ast.Index):
                        idx_node = idx_node.value

                    is_string_const = False
                    if isinstance(idx_node, ast.Constant) and isinstance(idx_node.value, str):
                        is_string_const = True
                    elif isinstance(idx_node, ast.Str):
                        is_string_const = True

                    if is_string_const:
                        prop_name = idx_node.value if isinstance(idx_node, ast.Constant) else idx_node.s

                        out_var = target_var if target_var else self._get_temp_var()
                        instrs.append({
                            "op": "attr",
                            "args": [obj_arg, self._create_str_arg(prop_name)],
                            "out": out_var
                        })
                        return self._create_var_arg(out_var), instrs

                    idx_arg, idx_instrs = self._process_expr(idx_node)
                    instrs.extend(idx_instrs)

                    out_var = target_var if target_var else self._get_temp_var()
                    instrs.append({
                        "op": "getitem",
                        "args": [obj_arg, idx_arg],
                        "out": out_var
                    })
                    return self._create_var_arg(out_var), instrs

        elif isinstance(node, ast.BoolOp):
            # Boolean Operation (and, or)
            op_type = type(node.op)
            values = node.values
            out_var = target_var if target_var else self._get_temp_var()

            def process_bool_op(values, target):
                if len(values) == 1:
                    v_arg, v_instrs = self._process_expr(values[0], target_var=target)

                    if v_arg is not None:
                        is_same_var = (v_arg['t'] == 'var' and v_arg['v'] == target)
                        if not is_same_var:
                            v_instrs.append({"op": "set", "args": [v_arg], "out": target})
                    return v_instrs

                head = values[0]
                head_arg, head_instrs = self._process_expr(head)

                if head_arg is not None:
                    is_same_var = (head_arg['t'] == 'var' and head_arg['v'] == target)
                    if not is_same_var:
                        head_instrs.append({"op": "set", "args": [head_arg], "out": target})

                target_arg = self._create_var_arg(target)

                rest_instrs = process_bool_op(values[1:], target)

                if op_type == ast.And:
                    if_op = {
                        "op": "if",
                        "args": [target_arg],
                        "then": rest_instrs,
                        "else": []
                    }
                    head_instrs.append(if_op)
                elif op_type == ast.Or:
                    if_op = {
                        "op": "if",
                        "args": [target_arg],
                        "then": [],
                        "else": rest_instrs
                    }
                    head_instrs.append(if_op)
                else:
                    raise ValueError(f"Unsupported BoolOp: {op_type}")

                return head_instrs

            return self._create_var_arg(out_var), process_bool_op(values, out_var)

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
            json.dump(result, f, indent=2, ensure_ascii=False)
        print(f"{input_filename} -> {output_filename}")
    except Exception as e:
        sys.stderr.write(f"Error: {e}\n")
        sys.exit(1)

if __name__ == '__main__':
    main()
