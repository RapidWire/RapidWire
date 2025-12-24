import * as vscode from 'vscode';
import {
    RapidWireCompletionProvider,
    RapidWireHoverProvider,
    RapidWireSignatureHelpProvider
} from './providers';

export function activate(context: vscode.ExtensionContext) {
    console.log('RapidWire extension is now active!');

    const completionProvider = vscode.languages.registerCompletionItemProvider(
        'rapidwire',
        new RapidWireCompletionProvider()
    );

    const hoverProvider = vscode.languages.registerHoverProvider(
        'rapidwire',
        new RapidWireHoverProvider()
    );

    const signatureProvider = vscode.languages.registerSignatureHelpProvider(
        'rapidwire',
        new RapidWireSignatureHelpProvider(),
        '(', ','
    );

    context.subscriptions.push(completionProvider, hoverProvider, signatureProvider);
}

export function deactivate() {}
