# Recipes, Targets and Scope Net — design

Date: 2026-10-22. Scope: Simple chat (`aiforge_core/runtime/chat_agent/`) and its API routing (`aiforge_core/api/routes/_chat/`). Team/pipeline runs are out of scope.

## Problem

Three complaints from the owner, each confirmed in code:

1. **Small, fixed jobs go through the model.** "Run the tests" or "compile" always ends in a model run. The model must notice a `workflows` block and choose `workflow_run`; the `project` and `run_tests` tools auto-detect the stack and ignore any project script. Nothing routes a short, known request to a known command without a model call.
2. **Goals the user pastes are lost.** When the user pastes expected output ("match this output"), the text is cut to 1200 chars in the goal pin and handoff, to 120 chars once folded by a condense, and survives in full only behind a `memory_lookup` id the model may never call. The board's "— check:" is free text; nothing ever compares an output with the target. The model later asks "can you paste it again?".
3. **Unasked work.** The system prompts say "state the assumption and continue". The guards that fire most (edit-claim, zero-edit, done-check) push the model toward more work. Nothing compares the final diff with the request.

Owner's decisions (asked 2026-10-22):

- Recipes are **proposed by AIForge after the first successful run**; the user confirms once.
- Target proof: the **model shows a compare; the harness checks that one ran and passed**.
- Extras: **do only the request; extras are listed as suggestions.**
- Forgetting bites most **inside the same chat on later turns**.

All three fixes are harness enforcement. Prompt text changes too, but no fix depends on the model obeying text.

## 1. Recipes

### Store

`<repo>/.aiforge/recipes.json`, one object per verb:

```json
{
  "test":    {"cmd": "./scripts/test.sh", "cwd": ".", "aliases": ["run tests", "pytest"], "saved": 1761100000},
  "build":   {"cmd": "mvn -q clean package -DskipTests", "cwd": "."},
  "run-job": {"cmd": "python main.py --job nightly", "cwd": "."}
}
```

- Built-in verbs: `test`, `build`, `run`, `lint`, `deploy`. Custom verbs are any slug the user names.
- `cwd` is relative to the repo root. `aliases` are extra bare phrases for the zero-LLM route.
- A chat with no pinned project keeps recipes under `<config>/chat-workspaces/session-N/.aiforge/recipes.json`.
- Module: `runtime/recipes.py` — `load(root)`, `get(root, verb)`, `save(root, verb, cmd, cwd, aliases)`, `remove(root, verb)`, `verb_for_command(root, cmd)`, `verb_for_phrase(root, text)`.
- The model never writes this file. Only the user's confirm (card click or explicit sentence) does.

### Capture

At FINAL, `recipes.offers(turn)` scans the turn's action log for commands with exit 0 that the existing `session_ledger.capture_working_workflow` classifier already calls build/test/run commands. For each verb that has no recipe yet, the answer gets a card:

> Save `mvn -q test` as this project's **test** recipe? [Save] [Not this]

- Card = a `recipe_offer` event on the SSE stream with `{verb, cmd, cwd}`; the web UI renders two buttons. `POST /api/chat/sessions/{id}/recipes` with `{verb, cmd, cwd, aliases?}` saves; "Not this" writes `verb` into `recipes-declined.json` in the same folder so the same command is not offered again this chat.
- One offer per verb per turn, at most three per turn.
- Explicit sentence: "remember: compile = ./build.sh", "always run tests with ./t.sh", "from now on build is make". The existing `rule_capture` cue regex fires; a new pre-check `recipes.parse_sentence(text)` claims the message before the rule classifier when it matches `<verb> (=|is|means|with|using) <command>`. The reply is the model-free "Got it — saved" path that `rule_capture` already uses. A command that does not exist on disk is still saved; the first run tells.

### Enforcement

1. **Tool handlers follow the recipe.** `_t_project` (`run`/`build`/`test`), `_t_run_tests` and `_t_workflow_run`: if `recipes.get(root, verb)` exists, the recipe command runs instead of auto-detection. The observation starts with `[recipe test] ./scripts/test.sh`. Auto-detect is only the fallback.
2. **Raw commands are replaced.** In `_t_run_command`, before the risk gate: `recipes.verb_for_command(root, cmd)` maps a command by family (`pytest`, `python -m pytest`, `mvn test`, `npm test`, `go test`, `cargo test`, `make test` → `test`; `mvn package|compile`, `npm run build`, `go build`, `cargo build`, `make` → `build`; …). If that verb has a recipe and the command is not the recipe itself, the recipe runs instead and the observation says `[recipe test replaced: pytest -q]`. `AIFORGE_CHAT_RECIPE_REPLACE=0` turns this into a warning line only. Commands that carry explicit paths or extra flags beyond the family pattern are not replaced (the model is running one test file on purpose).
3. **RECIPES block.** Priority area of the prompt (next to rules, never trimmed): one line per verb, `test → ./scripts/test.sh`. Cap 10 lines; more verbs are listed by name only.

### Zero-LLM route

In `API/_routing.py`, before the enhancer and the model run: `recipes.verb_for_phrase(root, text)` matches the whole message (after trimming punctuation and polite words) against fixed phrases per built-in verb (`run tests`, `run the tests`, `test`, `tests`, `compile`, `build`, `build it`, `run it`, `lint`, `deploy`) and each recipe's `aliases`. A custom verb matches its own name or `run <verb>`.

- Match + recipe exists: the recipe runs through the normal `_t_run_command` runner (approval, sandbox and action log apply). The answer is the command, the exit code and the output (tail 4000 chars, full in a collapsible). No model call. Facts line: `recipe test: exit 0, 3.2 s`.
- Exit non-zero: the output is handed to the model as the turn's request ("`test` recipe failed; fix the cause, do not rerun blindly"), same as `workflow_run` does today. This is the only case where the model runs.
- No recipe: normal path. The model will probably run something; the capture offer then appears.
- A message with more than the verb phrase ("run tests and fix the failures") goes to the model; the RECIPES block and the replace rule still apply.

### Edit and remove

- `/recipes` slash command: lists verbs, commands, aliases. Model-free, like `/help`.
- "forget the test recipe" / "remove recipe build": `recipes.parse_sentence` handles `forget|remove|drop … recipe`. Model-free ack.
- `AIFORGE_CHAT_RECIPES=0` disables store, capture, replace and route.

## 2. Targets

### Capture

A user message is a target when it has a cue and a payload.

- Cue regex (case-insensitive): `match (this|the|below|following) (output|input|result|response|data)`, `expected (output|result|response)`, `should (look|be) (like|exactly)`, `must (equal|match)`, `compare (with|against|to) this`, `set target`, `target:`.
- Payload: a fenced block, a paste of ≥ 200 chars after the cue sentence, or an uploaded file in the same message.
- Explicit form works without a payload size: "set target: output = ```…```".

The harness saves the payload verbatim to `<chat scratch>/targets/target-N.<ext>`; `ext` is sniffed (json, csv, xml, yaml, txt). The chat scratch folder already exists per chat and is never committed. Side-car `targets.json`:

```json
[{"id": "target-1", "label": "output", "path": "targets/target-1.json", "bytes": 4210,
  "sha256": "…", "turn": 3, "verified": null, "versions": ["targets/target-1.v1.json"]}]
```

- `label` comes from the cue noun (output, input, response, data) or the user's own word after "target:".
- Several targets per chat. An uploaded file is copied, not moved; `chat_media` keeps working as before.
- A later message with a cue and a payload and the same label ("actually match this new output") saves `target-1.v2.json`, points `path` at it, clears `verified`, keeps v1.
- Module: `runtime/targets.py` — `capture(session, text, uploads)`, `list(session)`, `block(session)`, `mark_verified(session, id, evidence)`, `is_compare(cmd, session)`.

### Never forgotten inside the chat

- **TARGETS block** in the priority area (same tier as rules and the board; never trimmed, re-pinned by `_repin_turn_note` after a condense and included in `_restart_block` and `handoff.seed_next_turn`). Per target: `target-1 output.json 4.2 KB — first 300 chars … — full: file_read targets/target-1.json — verified: no`.
- Prompt line in the block: "Target data is on disk. Read it with file_read. Never ask the user to paste or repeat it."
- The target file is never summarized. `chat_media.context_block` skips files that are targets (they are shown by the TARGETS block instead).
- **Re-ask catch.** A FINAL answer that asks the user for the target (regex: `paste .* again`, `share (the|your) expected`, `what (was|is) the (expected|data|output)`, `send .* (sample|expected)`) is sent back once with the message "The target is at targets/target-N.ext; read it, do not ask." If the second answer still asks, it is shown with a ⚠ line. This follows the RuleBreakGuard send-back pattern but needs no model call.

### Board

Each target adds an **enforced** item to the board at capture time: `output matches target-1 — check: compare command ran and passed`. It uses the enforced flag from the 10-11 board (`from_request` False, cannot be closed by `plan_progress` status).

- Close rule: the action log has a command the harness classes as a compare on that target — `diff`, `cmp`, `jq`, `csvdiff`, `python … (compare|assert|diff)`, `sort … | diff`, `git diff --no-index`, or any command whose text contains the target path together with one of those tools — with exit 0. `targets.is_compare(cmd, session)` decides. The item's evidence stores the command and the first 1000 chars of output. The facts line gets `target-1: matched`.
- Exit non-zero: item stays open; the model sees the diff as usual. The facts line gets `target-1: differs`.
- `_done_check` and `open_for_final`: an open enforced target item means the answer cannot be DONE; the reminder names the target and the compare rule.
- The model may still write `plan_progress status done` on the item; the harness ignores it with the note "target items close on a passing compare".
- Compare tool missing in the sandbox (e.g. `jq`): the model installs it (sandbox rule). `diff` and `python` are always present.

### Switches

`AIFORGE_CHAT_TARGETS=0` disables capture, block, re-ask catch and the enforced item. `AIFORGE_CHAT_TARGET_MIN_CHARS` (200) is the paste floor.

## 3. Scope net

### Prompt stance

In `_native_prompt.py`, `_prompt_text.py` and `_plan_llm._RULES`, replace "When a choice can be undone, or one default is reasonable, state the assumption and continue" with:

> Do only what the request says. If something else seems needed, do not do it; write it under "Suggested, not done" at the end of the answer. If the request is ambiguous and the choice changes the result, ask.

"PROVE IT RUNS" stays: compile and test after a change is part of doing the request. Build and test go through the recipe when one exists.

### Harness check at FINAL

`runtime/chat_agent/_guards/scope_net.py`, run in `_finish._handle_final` after the existing guards, no model call:

1. Changed files of the turn = the facts-line set (git diff names + untracked files the turn created), minus helper scripts already held by `chat_run_only`, minus `targets/` and `scratch/`.
2. Attached files = the union of file hashes recorded as item evidence by `_items` for items that are `from_request` or came from a user steer (`msg-N`).
3. Unattached = changed − attached. If not empty, the answer gets:

   > ⚠ Unrequested changes: `src/util.py`, `README.md` [Revert these]

   The button calls `POST /api/chat/sessions/{id}/revert` with the paths: `git checkout -- <path>` for tracked files, delete for untracked. Nothing is reverted without the click.
4. Items the model added itself (`plan_progress add` with no matching user cue, see below) count as unattached for their files.

### Board rule

`plan_progress add` from the model: `_tasks.apply_progress` tags the item `self_added` when neither `_same_work` maps it to a planned item nor the title shares a noun with the request or any steer. The dock shows it as "(added by agent)". Its files are unattached for the scope net. Tagging is deterministic (word overlap), no model call.

### Enhancer

`API/_stages.py` enhancer restatement: sentences in the restatement whose first verb is not in the original request (lemmatized imperative list: add, create, write, refactor, rename, remove, update, install, configure, migrate, deploy, test, document) are dropped before the restatement is appended. The restatement may clarify, never add steps. `AIFORGE_CHAT_ENHANCE_ADDS=1` restores the old behaviour.

### Session rules fix

Session-scope rules captured by `rule_capture` (`_SESSION_ITEMS`) are added to the rules block for that chat. Today they are captured and shown in the UI but never reach the model.

### Switch

`AIFORGE_CHAT_SCOPE_NET=0` disables the FINAL check and the revert button; the prompt stance and the enhancer change stay.

## 4. Data flow summary

```
user message
  ├─ recipes.parse_sentence  → save/remove recipe, model-free ack
  ├─ recipes.verb_for_phrase → run recipe, model-free answer (model only on failure)
  ├─ targets.capture         → targets/target-N.ext + enforced board item
  └─ model turn
       ├─ prompt: rules | RECIPES block | TARGETS block | board   (priority, never trimmed)
       ├─ tools: project/run_tests/workflow_run/run_command follow or replace with recipe
       ├─ items: target item closes only on a passing compare in the action log
       └─ FINAL: guards → scope_net (unattached files) → re-ask catch → recipe offers → done_check
```

## 5. Error handling

- Recipe command missing or exit ≠ 0 on the zero-LLM route: output shown, model takes over with the output. The recipe is not removed.
- `recipes.json` unreadable: logged, treated as empty; the capture offer can rewrite it.
- Target file write fails (disk, permissions): the message goes to the model unchanged with a ⚠ line in the answer; no enforced item.
- Compare command crashes (exit ≠ 0 and ≠ 1 for diff): counts as "differs".
- Revert endpoint: refuses paths outside the chat's work folders (reuses `scope_guard` roots).

## 6. Tests

Unit (pytest, `tests/python/`), switches on explicitly inside the tests since the conftest keeps new features off:

- `test_recipes.py`: store round-trip; `verb_for_command` families incl. the not-replaced cases (explicit paths, extra flags); `verb_for_phrase` phrases and aliases; `parse_sentence` save/forget forms; offer generation picks only exit-0 build/test commands and skips declined verbs; tool handlers use the recipe; replace in `_t_run_command` and the `REPLACE=0` warning mode; zero-LLM route answers without a model call and hands over on failure.
- `test_targets.py`: cue + payload detection incl. fenced, long paste, upload, explicit form, and non-matches; ext sniffing; versioning on a repeat cue; TARGETS block survives `_compact_convo`, `_restart_block` and `seed_next_turn`; `chat_media` skips target files; re-ask catch sends back once; enforced item closes only on `is_compare` + exit 0, ignores `plan_progress done`; `_done_check` blocked while open.
- `test_scope_net.py`: unattached file list from facts + item evidence; helper scripts and targets excluded; `self_added` tagging; revert endpoint tracked/untracked and the out-of-root refusal; enhancer drops added-verb sentences.
- `test_session_rules_in_prompt.py`: session items reach the rules block.

Suite: run the related chat suite on the nuc (not the Mac), as for the 10-15 work. Full suite before push.

Live rig on the nuc (pattern of `~/work/af-rules-live.sh`, qwen via :1234), one chat, four turns:

1. Paste an expected JSON with "the endpoint must match this output" and ask for the change. Check: `targets/target-1.json` written, TARGETS block in the prompt, enforced item on the board.
2. Press Compact, then "continue". Check: block still present, model reads the file, no re-paste ask.
3. "run tests" bare. Check: first time the model runs the tests and the offer card appears; after Save, a second "run tests" answers with zero model calls (the proxy log from `~/work/ctxrun/llmproxy.py` shows no request).
4. Steer with a small extra fix. Check: the target item closes only after a diff with exit 0; a file the model touched outside the request appears under "Unrequested changes" and the revert button restores it.

Code review: at least two rounds (owner rule), findings fixed before push.

## 7. Out of scope

- Team mode and the ticket pipeline.
- Cross-chat memory of targets (owner: same-chat is the pain).
- A model-free router for anything beyond bare recipe phrases.
- Recipes stored outside the repo (`<config>`) except for scratch chats.
