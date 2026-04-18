---
name: aiforge-crg
description: Code-review-graph — Python AST blast radius + dependency chain for any .py file or symbol in the repo. Use BEFORE touching shared code to know what breaks, and before approving a PR to validate the review scope.
version: 1.0.0
platforms: [macos]
---

# aiforge-crg

## Blast radius (upstream callers)

```bash
{{AIFORGE_PY}} - <<'PY'
import json
from pathlib import Path
from aiforge_core.crg import build_graph, blast_radius
g = build_graph(Path("."))
print(json.dumps(blast_radius(g, "aiforge_core/store.py::Store.transition", max_depth=3), indent=2))
PY
```

Returns `{files, affected_symbols, max_depth_reached}`.

## Dependency chain (callers + callees)

```bash
{{AIFORGE_PY}} - <<'PY'
import json
from pathlib import Path
from aiforge_core.crg import build_graph, dependency_chain
g = build_graph(Path("."))
print(json.dumps(dependency_chain(g, "aiforge_core/store.py::Store.transition"), indent=2))
PY
```

## Target formats

- `path/to/file.py` — all symbols in that file
- `path/to/file.py::function_name`
- `path/to/file.py::ClassName.method`

Non-Python files are ignored.
