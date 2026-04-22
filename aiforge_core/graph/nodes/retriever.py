from __future__ import annotations

from aiforge_core.runtime.feature_flags import get_flag

from ..state import AgentState


def retrieve_context(state: AgentState, role: str) -> list[dict]:
    flags = state.get("flags") or {}
    rag_backend = flags.get("rag.backend") or get_flag("rag.backend", "legacy")

    ticket = state.get("ticket") or {}
    query = f"{ticket.get('title', '')}\n{(ticket.get('body') or '')[:1000]}"

    if rag_backend == "llamaindex":
        try:
            from aiforge_core.rag.retriever import retrieve_for_role_li
            hits = retrieve_for_role_li(None, role, query, ticket.get("parent_id"))
            return [{"role": "system", "content": f"[RAG context]\n{h}" } for h in hits[:5]]
        except Exception:
            pass

    try:
        from aiforge_core.runtime.memory import Memory
        mem = Memory()
        hits = mem.search(query, role=role, top_k=5)
        return [{"role": "system", "content": f"[memory context]\n{h.text}"}
                for h in hits if hasattr(h, "text")]
    except Exception:
        return []


def inject_context(state: AgentState, role: str) -> AgentState:
    context_msgs = retrieve_context(state, role)
    if not context_msgs:
        return state
    messages = list(state.get("messages") or [])
    messages = context_msgs + messages
    return {**state, "messages": messages}
