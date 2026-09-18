// Styles use VS Code's theme variables, so the chat follows the editor theme.
export const STYLE = `
body { padding: 0; margin: 0; color: var(--vscode-foreground); font-family: var(--vscode-font-family);
  font-size: var(--vscode-font-size); }
#app { display: flex; flex-direction: column; height: 100vh; }
#log { flex: 1; overflow-y: auto; padding: 8px 10px; }
.empty { opacity: .7; padding: 12px 4px; line-height: 1.5; }
.turn { margin: 10px 0; }
.turn.user .bubble { background: var(--vscode-input-background); border: 1px solid var(--vscode-input-border, transparent);
  border-radius: 6px; padding: 6px 8px; white-space: pre-wrap; }
.answer p { margin: 6px 0; line-height: 1.5; }
.answer ul, .answer ol { margin: 6px 0; padding-left: 20px; }
.answer li { margin: 2px 0; line-height: 1.5; }
.answer ol > li + li { margin-top: 6px; }
.answer li > ul, .answer li > ol { margin: 4px 0 6px; padding-left: 18px; }
.answer pre, .preview pre { background: var(--vscode-textCodeBlock-background); padding: 6px; overflow-x: auto; border-radius: 4px; }
code { font-family: var(--vscode-editor-font-family); font-size: .95em; }
.steps { margin: 4px 0; opacity: .85; }
.steps summary { cursor: pointer; font-size: .9em; opacity: .8; }
.step { font-size: .9em; padding: 2px 0 2px 8px; border-left: 2px solid var(--vscode-panel-border); margin: 2px 0;
  white-space: pre-wrap; word-break: break-word; }
.step.thought { font-style: italic; opacity: .85; }
.step.sys { opacity: .65; }
.step.tool .mark { display: inline-block; width: 1em; }
.step.bad, .step.err { color: var(--vscode-errorForeground); }
.step .arg { opacity: .75; font-family: var(--vscode-editor-font-family); }
.role { font-size: .8em; padding: 0 5px; margin-right: 5px; border-radius: 8px; background: var(--vscode-badge-background);
  color: var(--vscode-badge-foreground); font-style: normal; }
.draft, .working { opacity: .6; font-size: .9em; font-style: italic; }
.ask { margin: 6px 0; padding: 6px 8px; border-left: 3px solid var(--vscode-focusBorder); }
.changes { margin: 8px 0; border: 1px solid var(--vscode-panel-border); border-radius: 6px; }
.changes-head { padding: 5px 8px; font-weight: 600; display: flex; gap: 8px; align-items: center; flex-wrap: wrap;
  border-bottom: 1px solid var(--vscode-panel-border); }
.file { border-bottom: 1px solid var(--vscode-panel-border); }
.file:last-child { border-bottom: 0; }
.filerow { display: flex; gap: 6px; align-items: center; padding: 4px 8px; flex-wrap: wrap; }
.path { font-family: var(--vscode-editor-font-family); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; min-width: 80px; }
.st { font-size: .75em; text-transform: uppercase; opacity: .75; }
.acts { display: flex; gap: 6px; }
.add { color: var(--vscode-gitDecoration-addedResourceForeground, #3fb950); }
.del { color: var(--vscode-gitDecoration-deletedResourceForeground, #f85149); }
.filediff summary { cursor: pointer; font-size: .85em; opacity: .75; padding: 0 8px 4px; }
.diff { font-family: var(--vscode-editor-font-family); font-size: .85em; overflow-x: auto; }
.dl { white-space: pre; padding: 0 8px; }
.dl.add { background: var(--vscode-diffEditor-insertedLineBackground, rgba(63,185,80,.15)); color: inherit; }
.dl.del { background: var(--vscode-diffEditor-removedLineBackground, rgba(248,81,73,.15)); color: inherit; }
.dl.hunk { opacity: .6; }
.approval { margin: 10px 0; padding: 8px; border: 1px solid var(--vscode-inputValidation-warningBorder);
  background: var(--vscode-inputValidation-warningBackground); border-radius: 6px; }
.approval .row { display: flex; gap: 10px; margin-top: 6px; }
.muted { opacity: .75; }
button { background: var(--vscode-button-background); color: var(--vscode-button-foreground); border: 0;
  padding: 4px 10px; border-radius: 3px; cursor: pointer; }
button:hover { background: var(--vscode-button-hoverBackground); }
button.link { background: none; color: var(--vscode-textLink-foreground); padding: 0; font-size: .9em; }
button.link:hover { text-decoration: underline; background: none; }
#composer { border-top: 1px solid var(--vscode-panel-border); padding: 8px 10px; }
#input { width: 100%; box-sizing: border-box; resize: vertical; background: var(--vscode-input-background);
  color: var(--vscode-input-foreground); border: 1px solid var(--vscode-input-border, transparent);
  font-family: inherit; font-size: inherit; padding: 6px; border-radius: 4px; }
.bar { display: flex; gap: 8px; align-items: center; margin-top: 6px; }
.spacer { flex: 1; }
select { background: var(--vscode-dropdown-background); color: var(--vscode-dropdown-foreground);
  border: 1px solid var(--vscode-dropdown-border); padding: 2px 4px; }
.error { color: var(--vscode-errorForeground); font-size: .9em; margin-bottom: 6px; white-space: pre-wrap; }
.notice { font-size: .9em; margin-bottom: 6px; white-space: pre-wrap; opacity: .85; }
.banner { margin: 4px 0 8px; padding: 6px 8px; border-left: 3px solid var(--vscode-focusBorder);
  background: var(--vscode-editorWidget-background); font-size: .9em; }
.turn.replaced { opacity: .45; }
`;
