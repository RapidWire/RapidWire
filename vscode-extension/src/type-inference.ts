import { sdkDefs } from './rapidwire-defs';

export function inferTypes(text: string): Map<string, string> {
    const variableTypes = new Map<string, string>();

    // Initialize with SDK variables
    for (const v of sdkDefs.variables) {
        variableTypes.set(v.name, v.type);
    }

    const lines = text.split('\n');

    // Regex for assignment: variable = expression
    // Captures: 1: variable name, 2: expression
    const assignmentRegex = /^\s*(\w+)\s*=\s*(.+)/;

    // Regex for function call: funcName(args...)
    // Captures: 1: function name
    const callRegex = /^(\w+)\s*\(/;

    for (const line of lines) {
        // Remove comments
        const cleanLine = line.split('#')[0];
        if (!cleanLine.trim()) continue;

        const match = cleanLine.match(assignmentRegex);
        if (match) {
            const varName = match[1];
            const rhs = match[2].trim();

            // 1. Check for function call
            const callMatch = rhs.match(callRegex);
            if (callMatch) {
                const funcName = callMatch[1];
                const funcDef = sdkDefs.functions.find(f => f.name === funcName);
                if (funcDef) {
                    variableTypes.set(varName, funcDef.returnType);
                    continue;
                }
            }

            // 2. Check for dictionary access
            if (rhs.startsWith('storage[')) {
                variableTypes.set(varName, 'str');
                continue;
            }

            // 3. Check for variable assignment (e.g., user = sender)
            // We check this after function call to avoid matching "sender" in "sender_func()" if that existed,
            // though here we look for exact match or property access.
            // Simple approach: if rhs is a known variable
            if (variableTypes.has(rhs)) {
                variableTypes.set(varName, variableTypes.get(rhs)!);
                continue;
            }

            // 3. Check for literals
            // Integer
            if (/^-?\d+$/.test(rhs)) {
                variableTypes.set(varName, 'int');
                continue;
            }
            // String
            if (/^["'].*["']$/.test(rhs)) {
                variableTypes.set(varName, 'str');
                continue;
            }
        }
    }

    return variableTypes;
}
