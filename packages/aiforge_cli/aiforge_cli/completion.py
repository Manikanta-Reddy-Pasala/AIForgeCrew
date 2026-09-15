"""Shell completion, generated from the command table.

A frozen one-file binary cannot be asked for completions cheaply — starting it
per keystroke would cost a PyInstaller unpack every TAB — so these scripts
carry the word lists inline. They are generated rather than written by hand so
that a new command in commands.py is completable in every shell at once.
"""

from __future__ import annotations

from . import commands as tbl

_DIR_ARGS = {tbl.ARG_DIR, tbl.ARG_FILE}


def _words() -> str:
    return " ".join(tbl.top_names())


def _sub(cmd: tbl.Command) -> str:
    return " ".join(cmd.choices)


def bash() -> str:
    cases = []
    for cmd in tbl.TOP:
        if cmd.choices:
            cases.append(f'    {cmd.name}) COMPREPLY=( $(compgen -W "{_sub(cmd)}" -- "$cur") ) ;;')
        elif cmd.arg == tbl.ARG_COMMAND:
            cases.append(f'    {cmd.name}) COMPREPLY=( $(compgen -W "{_words()}" -- "$cur") ) ;;')
        elif cmd.arg in _DIR_ARGS:
            cases.append(f'    {cmd.name}) COMPREPLY=( $(compgen -d -- "$cur") ) ;;')
    body = "\n".join(cases)
    return f"""# aiforge completion for bash — generated, do not edit.
_aiforge() {{
  local cur prev
  cur="${{COMP_WORDS[COMP_CWORD]}}"
  prev="${{COMP_WORDS[COMP_CWORD-1]}}"
  if [ "$COMP_CWORD" -eq 1 ]; then
    COMPREPLY=( $(compgen -W "{_words()}" -- "$cur") )
    return
  fi
  case "${{COMP_WORDS[1]}}" in
{body}
    *) COMPREPLY=( $(compgen -f -- "$cur") ) ;;
  esac
}}
complete -F _aiforge aiforge
"""


def zsh() -> str:
    lines = [f"  '{c.name}:{c.help}'" for c in tbl.TOP]
    subs = []
    for cmd in tbl.TOP:
        if cmd.choices:
            subs.append(f"      {cmd.name}) _values '{cmd.name}' {' '.join(cmd.choices)} ;;")
        elif cmd.arg in _DIR_ARGS:
            subs.append(f"      {cmd.name}) _files -/ ;;")
        elif cmd.arg == tbl.ARG_COMMAND:
            subs.append(f"      {cmd.name}) _values command {' '.join(tbl.top_names())} ;;")
    return """#compdef aiforge
# aiforge completion for zsh — generated, do not edit.
_aiforge() {
  local -a cmds
  cmds=(
%s
  )
  if (( CURRENT == 2 )); then
    _describe -t commands 'aiforge command' cmds
    return
  fi
  case "${words[2]}" in
%s
      *) _files ;;
  esac
}
_aiforge "$@"
""" % ("\n".join(lines), "\n".join(subs))


def fish() -> str:
    out = ["# aiforge completion for fish — generated, do not edit.",
           "complete -c aiforge -f"]
    for cmd in tbl.TOP:
        out.append(f"complete -c aiforge -n '__fish_use_subcommand' "
                   f"-a {cmd.name} -d '{cmd.help}'")
        if cmd.choices:
            out.append(f"complete -c aiforge -n '__fish_seen_subcommand_from {cmd.name}' "
                       f"-a '{_sub(cmd)}'")
        elif cmd.arg in _DIR_ARGS:
            out.append(f"complete -c aiforge -n '__fish_seen_subcommand_from {cmd.name}' -F")
    for flag, text in tbl.GLOBAL_FLAGS:
        long = [f for f in flag.replace(",", " ").split() if f.startswith("--")]
        if long:
            out.append(f"complete -c aiforge -l {long[0].lstrip('-')} -d '{text}'")
    return "\n".join(out) + "\n"


def powershell() -> str:
    top = ", ".join(f"'{c.name}'" for c in tbl.TOP)
    branches = []
    for cmd in tbl.TOP:
        if cmd.choices:
            vals = ", ".join(f"'{v}'" for v in cmd.choices)
            branches.append(f"      '{cmd.name}' {{ {vals} }}")
    return f"""# aiforge completion for PowerShell — generated, do not edit.
Register-ArgumentCompleter -Native -CommandName aiforge -ScriptBlock {{
  param($wordToComplete, $commandAst, $cursorPosition)
  $tokens = $commandAst.CommandElements | ForEach-Object {{ $_.ToString() }}
  $sub = if ($tokens.Count -gt 1) {{ $tokens[1] }} else {{ '' }}
  $items = if ($tokens.Count -le 2) {{ @({top}) }} else {{
    switch ($sub) {{
{chr(10).join(branches)}
      default {{ @() }}
    }}
  }}
  $items | Where-Object {{ $_ -like "$wordToComplete*" }} | ForEach-Object {{
    [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterValue', $_)
  }}
}}
"""


SHELLS = {"bash": bash, "zsh": zsh, "fish": fish, "powershell": powershell}


def script(shell: str) -> str:
    try:
        return SHELLS[shell]()
    except KeyError:
        raise SystemExit(f"aiforge completion: unknown shell '{shell}' "
                         f"(try: {', '.join(SHELLS)})") from None
