"""Append-only tables, SRS version freeze/state machine, child-table locks.

Revision ID: 0002
Revises: 0001
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

APPEND_ONLY = ["requirement_history", "audit_logs", "workflow_event_log"]
CHILDREN = ["srs_sections", "srs_version_requirements"]

FUNCTIONS = r"""
CREATE OR REPLACE FUNCTION forbid_mutation() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION '% is append-only (% not allowed)', TG_TABLE_NAME, TG_OP
    USING ERRCODE = 'integrity_constraint_violation';
END $$ LANGUAGE plpgsql;

-- approvals: append-only, except final_srs_version_id may be set once (NULL -> value).
CREATE OR REPLACE FUNCTION approvals_guard() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'approvals is append-only' USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  IF OLD.final_srs_version_id IS NOT NULL
     OR (to_jsonb(NEW) - 'final_srs_version_id') IS DISTINCT FROM (to_jsonb(OLD) - 'final_srs_version_id') THEN
    RAISE EXCEPTION 'approvals is append-only (only final_srs_version_id may be set once)'
      USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

-- consents: never deleted; only the revoke fields may be set once.
CREATE OR REPLACE FUNCTION consents_guard() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'project_llm_consents rows cannot be deleted'
      USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  IF OLD.revoked_at IS NOT NULL
     OR (to_jsonb(NEW) - ARRAY['revoked_at','revoked_by','revocation_reason'])
        IS DISTINCT FROM (to_jsonb(OLD) - ARRAY['revoked_at','revoked_by','revocation_reason']) THEN
    RAISE EXCEPTION 'consent rows are immutable except a one-time revoke'
      USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

-- srs_versions: artifact columns writable only while 'generating'; allowlisted transitions only.
CREATE OR REPLACE FUNCTION srs_versions_guard() RETURNS trigger AS $$
DECLARE
  lifecycle text[] := ARRAY['status','sent_at','approved_at','superseded_at','finalized_at'];
  allowed boolean;
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'srs_versions rows cannot be deleted' USING ERRCODE = 'integrity_constraint_violation';
  END IF;

  IF (jsonb_build_object('a', NEW.id, 'b', NEW.project_id, 'c', NEW.seq, 'd', NEW.version_label, 'e', NEW.kind)
      IS DISTINCT FROM
      jsonb_build_object('a', OLD.id, 'b', OLD.project_id, 'c', OLD.seq, 'd', OLD.version_label, 'e', OLD.kind)) THEN
    RAISE EXCEPTION 'srs_versions identity columns are immutable' USING ERRCODE = 'integrity_constraint_violation';
  END IF;

  IF OLD.status <> 'generating' THEN
    -- Frozen: only lifecycle columns may change, and write-once timestamps never rewrite.
    IF (to_jsonb(NEW) - lifecycle) IS DISTINCT FROM (to_jsonb(OLD) - lifecycle) THEN
      RAISE EXCEPTION 'srs_versions artifact columns are frozen after leaving generating'
        USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF (OLD.sent_at IS NOT NULL AND NEW.sent_at IS DISTINCT FROM OLD.sent_at)
       OR (OLD.approved_at IS NOT NULL AND NEW.approved_at IS DISTINCT FROM OLD.approved_at)
       OR (OLD.superseded_at IS NOT NULL AND NEW.superseded_at IS DISTINCT FROM OLD.superseded_at)
       OR (OLD.finalized_at IS NOT NULL AND NEW.finalized_at IS DISTINCT FROM OLD.finalized_at)
       OR (OLD.ready_at IS NOT NULL AND NEW.ready_at IS DISTINCT FROM OLD.ready_at) THEN
      RAISE EXCEPTION 'srs_versions timestamps are write-once' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
  END IF;

  IF NEW.status IS DISTINCT FROM OLD.status THEN
    allowed := (OLD.status, NEW.status) IN (
      ('generating','ready'), ('generating','final'), ('generating','qa_failed'),
      ('generating','failed'), ('ready','sent'), ('ready','superseded'),
      ('sent','superseded'), ('sent','approved'));
    IF NOT allowed THEN
      RAISE EXCEPTION 'illegal srs_versions transition % -> %', OLD.status, NEW.status
        USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.kind = 'final' AND NEW.status IN ('ready','sent','approved','superseded') THEN
      RAISE EXCEPTION 'a final version cannot take draft states' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.kind = 'draft' AND NEW.status = 'final' THEN
      RAISE EXCEPTION 'a draft version cannot become final' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.status IN ('ready','final') THEN
      IF NEW.docx_sha256 IS NULL OR NEW.pdf_sha256 IS NULL OR NEW.content_hash IS NULL
         OR NEW.substantive_hash IS NULL OR NEW.requirement_set_hash IS NULL
         OR NEW.qa_report IS NULL OR COALESCE(NEW.qa_report->>'passed','false') <> 'true' THEN
        RAISE EXCEPTION 'cannot leave generating: artifact columns incomplete or QA not passed'
          USING ERRCODE = 'integrity_constraint_violation';
      END IF;
    END IF;
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

-- children of a version can change only while the parent is generating.
CREATE OR REPLACE FUNCTION srs_children_guard() RETURNS trigger AS $$
DECLARE parent_status text; pid uuid; vid uuid;
BEGIN
  IF TG_OP = 'DELETE' THEN pid := OLD.project_id; vid := OLD.srs_version_id;
  ELSE pid := NEW.project_id; vid := NEW.srs_version_id; END IF;
  SELECT status::text INTO parent_status FROM srs_versions WHERE project_id = pid AND id = vid;
  IF parent_status IS DISTINCT FROM 'generating' THEN
    RAISE EXCEPTION '% rows are frozen once the version leaves generating', TG_TABLE_NAME
      USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  RETURN COALESCE(NEW, OLD);
END $$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    op.execute(FUNCTIONS)
    for table in APPEND_ONLY:
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION forbid_mutation()"
        )
    op.execute("CREATE TRIGGER approvals_append_only BEFORE UPDATE OR DELETE ON approvals "
               "FOR EACH ROW EXECUTE FUNCTION approvals_guard()")
    op.execute("CREATE TRIGGER consents_guard BEFORE UPDATE OR DELETE ON project_llm_consents "
               "FOR EACH ROW EXECUTE FUNCTION consents_guard()")
    op.execute("CREATE TRIGGER srs_versions_guard BEFORE UPDATE OR DELETE ON srs_versions "
               "FOR EACH ROW EXECUTE FUNCTION srs_versions_guard()")
    for table in CHILDREN:
        op.execute(
            f"CREATE TRIGGER {table}_guard BEFORE INSERT OR UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION srs_children_guard()"
        )


def downgrade() -> None:
    for table in CHILDREN:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_guard ON {table}")
    op.execute("DROP TRIGGER IF EXISTS srs_versions_guard ON srs_versions")
    op.execute("DROP TRIGGER IF EXISTS consents_guard ON project_llm_consents")
    op.execute("DROP TRIGGER IF EXISTS approvals_append_only ON approvals")
    for table in APPEND_ONLY:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
    for fn in ("srs_children_guard", "srs_versions_guard", "consents_guard", "approvals_guard",
               "forbid_mutation"):
        op.execute(f"DROP FUNCTION IF EXISTS {fn}()")
