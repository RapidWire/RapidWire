# RapidWire VS Code Extension

This extension provides auto-completion, hover information, and signature help for the RapidWire SDK in Python files, without requiring you to import the SDK explicitly.

## Features

- **Auto-completion**: Suggests RapidWire system variables (`sender`, `storage`, etc.) and functions (`output`, `transfer`, etc.) as you type.
- **Hover Info**: Shows type information and signatures when you hover over RapidWire symbols.
- **Signature Help**: Shows parameter hints when you type a function call (e.g., `output(...)`).

## Usage

1. Open a RapidWire contract file (`.rwc`).
2. Start typing a RapidWire function or variable name.
3. The extension will automatically provide suggestions.

## Installation from Source

To install this extension from the source code:

1. Ensure you have `npm` and `vsce` installed.
2. Run `npm install` in this directory.
3. Run `npm run compile` to build the extension.
4. Run `npx vsce package` to create a `.vsix` file.
5. Install the `.vsix` file in VS Code (`Extensions -> ... -> Install from VSIX`).

## Development

- Run `npm install` to install dependencies.
- Open the project in VS Code.
- Press `F5` to start debugging the extension.
