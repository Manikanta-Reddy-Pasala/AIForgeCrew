from __future__ import annotations

import os

from langgraph.graph import END, StateGraph

from .edges import after_doer, after_feedback, route_from_supervisor
from .nodes.doer import doer_node
from .nodes.feedback import feedback_node
from .nodes.learner import learner_node
from .nodes.planner import planner_node
from .nodes.supervisor import supervisor_node
from .state import AgentState


def build_graph():
    builder = StateGraph(AgentState)

    builder.add_node("supervisor_node", supervisor_node)
    builder.add_node("planner_node", planner_node)
    builder.add_node("doer_node", doer_node)
    builder.add_node("feedback_node", feedback_node)
    builder.add_node("learner_node", learner_node)

    builder.set_entry_point("supervisor_node")

    builder.add_conditional_edges(
        "supervisor_node",
        route_from_supervisor,
        {
            "planner_node": "planner_node",
            "doer_node": "doer_node",
            "learner_node": "learner_node",
            END: END,
        },
    )

    builder.add_edge("planner_node", "doer_node")

    builder.add_conditional_edges(
        "doer_node",
        after_doer,
        {
            "feedback_node": "feedback_node",
            "planner_node": "planner_node",
            END: END,
        },
    )

    builder.add_conditional_edges(
        "feedback_node",
        after_feedback,
        {
            "learner_node": "learner_node",
            "doer_node": "doer_node",
            "supervisor_node": "supervisor_node",
            END: END,
        },
    )

    builder.add_edge("learner_node", END)

    dsn = os.environ.get("AIFORGE_DSN", "")
    if dsn:
        try:
            from langgraph.checkpoint.postgres import PostgresSaver
            checkpointer = PostgresSaver.from_conn_string(dsn)
            return builder.compile(checkpointer=checkpointer)
        except Exception:
            pass

    return builder.compile()
