# Eval fixtures (F1..F7)

Each fixture: `ticket.json`, `allowed.txt`, `golden.json`, optional
`repo/` subtree. Runner: `scripts/evals/run_eval_suite.py`.

| ID | Scope | Difficulty | Stack |
|---|---|---|---|
| F1 | 1 method add | smoke | Java POJO |
| F2 | 2 files, getter+caller | small | Java |
| F3 | 3 files w/ test | medium | Java + JUnit |
| F4 | 5 files endpoint impl | hard | Spring Boot |
| F5 | refactor existing | medium | Java |
| F6 | bugfix (failing test) | medium | Java |
| F7 | endpoint + DTO + service + repo + test | full chain | Spring Boot |

Set `AIFORGE_EVAL_SMOKE=1` to run F1+F3 only (CI default).
Full suite via `--name F1 --name F2 ...` or omit `--name` for all.
