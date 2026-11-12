"""All graph nodes: ingestion branch + SRS/email/approval branch."""
from app.workflows.nodes_ingest import make_ingest_nodes
from app.workflows.nodes_srs import make_srs_nodes

__all__ = ["make_ingest_nodes", "make_srs_nodes"]
