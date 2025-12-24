import ast
import json
import os
import sys

# Add parent directory to path to find RapidWire module if needed
SDK_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'RapidWire', 'sdk.py')
OUTPUT_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'vscode-rapidwire', 'src', 'rapidwire-defs.ts')

def parse_sdk():
    with open(SDK_PATH, 'r', encoding='utf-8') as f:
        source = f.read()

    tree = ast.parse(source)

    definitions = {
        "variables": [],
        "functions": [],
        "classes": []
    }

    for node in tree.body:
        # Variables (AnnAssign)
        if isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                name = node.target.id
                type_hint = ast.unparse(node.annotation)
                definitions["variables"].append({
                    "name": name,
                    "type": type_hint,
                    "doc": ""
                })

        # Functions
        elif isinstance(node, ast.FunctionDef):
            name = node.name

            # Extract args
            args = []
            for arg in node.args.args:
                arg_name = arg.arg
                arg_type = ast.unparse(arg.annotation) if arg.annotation else "Any"
                args.append({"name": arg_name, "type": arg_type})

            # Extract return type
            ret_type = ast.unparse(node.returns) if node.returns else "None"

            # Docstring
            doc = ast.get_docstring(node) or ""

            definitions["functions"].append({
                "name": name,
                "args": args,
                "returnType": ret_type,
                "doc": doc
            })

        # Classes
        elif isinstance(node, ast.ClassDef):
            name = node.name
            doc = ast.get_docstring(node) or ""

            fields = []
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    fields.append({
                        "name": item.target.id,
                        "type": ast.unparse(item.annotation)
                    })

            definitions["classes"].append({
                "name": name,
                "doc": doc,
                "fields": fields
            })

    return definitions

def main():
    try:
        defs = parse_sdk()
        os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)

        # Write as TypeScript
        json_str = json.dumps(defs, indent=2)
        ts_content = f"export const sdkDefs = {json_str};\n"

        with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
            f.write(ts_content)
        print(f"Successfully generated definitions at {OUTPUT_PATH}")
    except Exception as e:
        print(f"Error generating definitions: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
