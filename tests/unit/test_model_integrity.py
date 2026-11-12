"""Schema-level checks that do not need a running database."""
from app.database.base import Base
from app.database.models import ProjectContact  # noqa: F401  (registers models)


def _fk_targets(table_name: str) -> set[tuple[tuple[str, ...], tuple[str, ...]]]:
    table = Base.metadata.tables[table_name]
    return {
        (tuple(c.name for c in fk.columns), tuple(e.target_fullname for e in fk.elements))
        for fk in table.foreign_key_constraints
    }


def test_project_contacts_uses_composite_client_scoped_fks():
    targets = _fk_targets("project_contacts")
    assert (
        ("project_id", "client_id"),
        ("projects.id", "projects.client_id"),
    ) in targets
    assert (
        ("client_id", "contact_id"),
        ("client_contacts.client_id", "client_contacts.id"),
    ) in targets


def test_projects_has_unique_id_client_for_composite_fk():
    table = Base.metadata.tables["projects"]
    uniques = {tuple(c.name for c in u.columns) for u in table.constraints if hasattr(u, "columns")}
    assert ("id", "client_id") in uniques


def test_active_consent_is_partial_unique():
    table = Base.metadata.tables["project_llm_consents"]
    idx = next(i for i in table.indexes if i.name == "uq_consent_active")
    assert idx.unique
