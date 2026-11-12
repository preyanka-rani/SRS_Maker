"""Graph assembly: ingest -> extract -> generate SRS; email -> feedback -> revise / approve."""
from langgraph.graph import END, START, StateGraph

from app.workflows.deps import WorkflowDeps
from app.workflows.nodes_ingest import HELD, make_ingest_nodes
from app.workflows.nodes_srs import make_srs_nodes
from app.workflows.state import SrsWorkflowState


def _trigger_type(state: SrsWorkflowState) -> str:
    return (state.get("trigger") or {}).get("type") or ""


def _route_after_event(state: SrsWorkflowState) -> str:
    return {"document": "consent_gate", "email": "consent_gate",
            "manual": "apply_manual_decision"}.get(state.get("route") or "", "await_client")


def _route_after_consent(state: SrsWorkflowState) -> str:
    if state.get("route") == HELD:
        return END
    return "process_email" if _trigger_type(state) == "email" else "extract_and_persist"


def _route_after_email(state: SrsWorkflowState) -> str:
    return {"approval": "process_approval", "changed": "generate_srs"}.get(
        state.get("route") or "", "await_client")


def _route_after_approval(state: SrsWorkflowState) -> str:
    return "generate_srs" if state.get("route") == "changed" else "await_client"


def _route_after_manual(state: SrsWorkflowState) -> str:
    return "generate_srs" if state.get("route") == "changed" else "await_client"


def build_graph(deps: WorkflowDeps, checkpointer):
    nodes = {**make_ingest_nodes(deps), **make_srs_nodes(deps)}
    graph = StateGraph(SrsWorkflowState)
    for name, fn in nodes.items():
        graph.add_node(name, fn)
    graph.add_edge(START, "load_project")
    graph.add_edge("load_project", "route_event")
    graph.add_conditional_edges("route_event", _route_after_event, {
        "consent_gate": "consent_gate", "apply_manual_decision": "apply_manual_decision",
        "await_client": "await_client"})
    graph.add_conditional_edges("consent_gate", _route_after_consent, {
        "extract_and_persist": "extract_and_persist", "process_email": "process_email", END: END})
    graph.add_edge("extract_and_persist", "generate_srs")
    graph.add_conditional_edges("process_email", _route_after_email, {
        "process_approval": "process_approval", "generate_srs": "generate_srs",
        "await_client": "await_client"})
    graph.add_conditional_edges("process_approval", _route_after_approval, {
        "generate_srs": "generate_srs", "await_client": "await_client"})
    graph.add_conditional_edges("apply_manual_decision", _route_after_manual, {
        "generate_srs": "generate_srs", "await_client": "await_client"})
    graph.add_edge("generate_srs", "await_client")
    graph.add_edge("await_client", "load_project")
    return graph.compile(checkpointer=checkpointer)
