import * as vscode from 'vscode';
import { sdkDefs } from './rapidwire-defs';
import { inferTypes } from './type-inference';

export class RapidWireCompletionProvider implements vscode.CompletionItemProvider {
    public provideCompletionItems(
        document: vscode.TextDocument,
        position: vscode.Position
    ): vscode.ProviderResult<vscode.CompletionItem[]> {

        const items: vscode.CompletionItem[] = [];

        // Add variables (inferred + built-in)
        const inferredTypes = inferTypes(document.getText());
        for (const [name, type] of inferredTypes) {
            // check if it's already in sdkDefs to get better docs if available
            const sdkVar = sdkDefs.variables.find(v => v.name === name);

            const item = new vscode.CompletionItem(name, vscode.CompletionItemKind.Variable);
            item.detail = type;
            if (sdkVar) {
                item.documentation = new vscode.MarkdownString(sdkVar.doc || `RapidWire System Variable: ${name}`);
            } else {
                item.documentation = new vscode.MarkdownString(`User variable: ${name}`);
            }
            items.push(item);
        }

        // Add functions
        for (const f of sdkDefs.functions) {
            const item = new vscode.CompletionItem(f.name, vscode.CompletionItemKind.Function);
            item.detail = `(${f.args.map(a => a.name + ": " + a.type).join(', ')}) -> ${f.returnType}`;
            item.documentation = new vscode.MarkdownString(f.doc || `RapidWire Function: ${f.name}`);
            // Add snippet for function call
            item.insertText = new vscode.SnippetString(`${f.name}($0)`);
            items.push(item);
        }

        // Add classes
        for (const c of sdkDefs.classes) {
            const item = new vscode.CompletionItem(c.name, vscode.CompletionItemKind.Class);
            item.documentation = new vscode.MarkdownString(c.doc);
            items.push(item);
        }

        return items;
    }
}

export class RapidWireHoverProvider implements vscode.HoverProvider {
    public provideHover(
        document: vscode.TextDocument,
        position: vscode.Position
    ): vscode.ProviderResult<vscode.Hover> {

        const range = document.getWordRangeAtPosition(position);
        if (!range) { return undefined; }

        const word = document.getText(range);

        // Check variables (using inference)
        const inferredTypes = inferTypes(document.getText());
        if (inferredTypes.has(word)) {
            const type = inferredTypes.get(word) || 'Any';
            // Check if it's a built-in variable to get documentation
            const v = sdkDefs.variables.find(x => x.name === word);
            const doc = v ? v.doc : '';

            return new vscode.Hover([
                new vscode.MarkdownString().appendCodeblock(type, 'rapidwire'),
                doc
            ]);
        }

        // Check functions
        const f = sdkDefs.functions.find(x => x.name === word);
        if (f) {
            const sig = `def ${f.name}(${f.args.map(a => a.name + ": " + a.type).join(', ')}) -> ${f.returnType}`;
            return new vscode.Hover([
                new vscode.MarkdownString().appendCodeblock(sig, 'python'),
                f.doc
            ]);
        }

        // Check classes
        const c = sdkDefs.classes.find(x => x.name === word);
        if (c) {
             return new vscode.Hover([
                new vscode.MarkdownString(`class **${c.name}**`),
                c.doc
            ]);
        }

        return undefined;
    }
}

export class RapidWireSignatureHelpProvider implements vscode.SignatureHelpProvider {
    public provideSignatureHelp(
        document: vscode.TextDocument,
        position: vscode.Position
    ): vscode.ProviderResult<vscode.SignatureHelp> {

        const linePrefix = document.lineAt(position).text.substr(0, position.character);

        let openParenIndex = -1;
        let nestedParens = 0;

        for (let i = linePrefix.length - 1; i >= 0; i--) {
            const char = linePrefix[i];
            if (char === ')') {
                nestedParens++;
            } else if (char === '(') {
                if (nestedParens > 0) {
                    nestedParens--;
                } else {
                    openParenIndex = i;
                    break;
                }
            }
        }

        if (openParenIndex === -1) { return undefined; }

        const prefix = linePrefix.substring(0, openParenIndex).trim();
        const match = prefix.match(/[\w]+$/);
        if (!match) { return undefined; }

        const funcName = match[0];
        const funcDef = sdkDefs.functions.find(f => f.name === funcName);

        if (!funcDef) { return undefined; }

        const signature = new vscode.SignatureInformation(
            `${funcDef.name}(${funcDef.args.map(a => a.name + ": " + a.type).join(', ')}) -> ${funcDef.returnType}`,
            new vscode.MarkdownString(funcDef.doc)
        );

        signature.parameters = funcDef.args.map(a =>
            new vscode.ParameterInformation(a.name)
        );

        const help = new vscode.SignatureHelp();
        help.signatures = [signature];
        help.activeSignature = 0;

        const argsText = linePrefix.substring(openParenIndex + 1);
        let commaCount = 0;
        let pLevel = 0;
        for (const char of argsText) {
             if (char === '(') pLevel++;
             else if (char === ')') pLevel--;
             else if (char === ',' && pLevel === 0) commaCount++;
        }

        help.activeParameter = commaCount;

        return help;
    }
}
