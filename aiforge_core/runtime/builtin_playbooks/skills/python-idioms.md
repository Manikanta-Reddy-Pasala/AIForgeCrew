---
name: python-idioms
description: Write clean, idiomatic, safe Python
triggers: [python, pythonic, type hints, venv, pep8, pytest, dataclass]
source: builtin
---

- **Pythonic over clever**: comprehensions, context managers (`with`), unpacking, `enumerate`/`zip`, EAFP (try/except) over LBYL where it reads cleaner.
- **Type hints** on public functions; run `mypy`/`pyright`. Use `dataclass`/`pydantic` for structured data, not bare dicts.
- **Mutable default args are a trap**: `def f(x=[])` shares state — use `None` + assign inside.
- **Truthiness vs `is None`**: distinguish `0`/`""`/`[]` (falsy) from `None`. Use `x is None`, not `not x`, when zero/empty are valid.
- **Resource safety**: `with open(...)`, close connections; prefer `pathlib` over string paths.
- **Isolated env** (`venv`/`uv`); pin deps; never `pip install` into system Python.
- **Errors**: raise specific exceptions; don't `except:` bare (catch `Exception` at most, and re-raise or handle).
- Format/lint with `ruff`/`black`; test with `pytest`.
