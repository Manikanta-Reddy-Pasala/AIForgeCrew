# DEPRECATED — `aiforge_core.doer`

Pre-archetype top-level doer package. Replaced by the pluggable
archetype at `aiforge_core/aiforge_agents/archetypes/doer.py`
(or the closest equivalent — see the archetype docs).

**Do not import from here.** Held only because some legacy
`runtime/` modules (`api.py` chat endpoint, `graph_runner.py`,
`adk_workflow.py`) still reference it. Will be deleted once those
runtime paths migrate to `aiforge_agents.orchestrator.run_ticket`.

Deprecation moved smolagents / langgraph / google-adk to
`pyproject.[project.optional-dependencies].legacy`. Install via
`pip install -e .[legacy]` if you need this code path.
