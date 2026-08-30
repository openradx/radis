"""Statement-level triggers maintaining the ReportSearchIndex search projection.

Triggers rather than Django signals because group_ids is access-control data:
a signal fires only for ORM writes, missing management commands, the admin,
and raw SQL. A trigger holds inside the same transaction as the write, for
every writer.

Statement-level rather than row-level because the bulk-upsert endpoint writes
membership with bulk_create (reports/api/viewsets.py:215,229), so one statement
can carry a whole batch.

AFTER, never BEFORE: PostgreSQL does not expose stored generated column values
to BEFORE triggers, so patient_age would mirror as NULL.
"""

from django.db import migrations

SYNC_GROUP_IDS = """
CREATE OR REPLACE FUNCTION pgsearch_sync_group_ids() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    UPDATE pgsearch_reportsearchindex rsi
    SET group_ids = COALESCE(
        (SELECT array_agg(g.group_id ORDER BY g.group_id)
           FROM reports_report_groups g WHERE g.report_id = rsi.report_id),
        '{}')
    WHERE rsi.report_id IN (SELECT DISTINCT report_id FROM changed);
    RETURN NULL;
END $$;
"""

SYNC_MODALITY_CODES = """
CREATE OR REPLACE FUNCTION pgsearch_sync_modality_codes() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    UPDATE pgsearch_reportsearchindex rsi
    SET modality_codes = COALESCE(
        (SELECT array_agg(m.code ORDER BY m.code)
           FROM reports_report_modalities rm
           JOIN reports_modality m ON m.id = rm.modality_id
          WHERE rm.report_id = rsi.report_id),
        '{}')
    WHERE rsi.report_id IN (SELECT DISTINCT report_id FROM changed);
    RETURN NULL;
END $$;
"""

SYNC_REPORT_FIELDS = """
CREATE OR REPLACE FUNCTION pgsearch_sync_report_fields() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    UPDATE pgsearch_reportsearchindex rsi
    SET language_code = l.code,
        patient_sex = c.patient_sex,
        patient_age = c.patient_age,
        patient_id = c.patient_id,
        study_datetime = c.study_datetime,
        study_description = c.study_description,
        report_created_at = c.created_at,
        report_updated_at = c.updated_at
    FROM changed c
    LEFT JOIN reports_language l ON l.id = c.language_id
    WHERE rsi.report_id = c.id
      AND (rsi.language_code, rsi.patient_sex, rsi.patient_age, rsi.patient_id,
           rsi.study_datetime, rsi.study_description, rsi.report_created_at,
           rsi.report_updated_at)
          IS DISTINCT FROM
          (l.code, c.patient_sex, c.patient_age, c.patient_id,
           c.study_datetime, c.study_description, c.created_at, c.updated_at);
    RETURN NULL;
END $$;
"""

CREATE_TRIGGERS = """
CREATE TRIGGER pgsearch_group_ids_ins AFTER INSERT ON reports_report_groups
REFERENCING NEW TABLE AS changed
FOR EACH STATEMENT EXECUTE FUNCTION pgsearch_sync_group_ids();

CREATE TRIGGER pgsearch_group_ids_del AFTER DELETE ON reports_report_groups
REFERENCING OLD TABLE AS changed
FOR EACH STATEMENT EXECUTE FUNCTION pgsearch_sync_group_ids();

CREATE TRIGGER pgsearch_modality_codes_ins AFTER INSERT ON reports_report_modalities
REFERENCING NEW TABLE AS changed
FOR EACH STATEMENT EXECUTE FUNCTION pgsearch_sync_modality_codes();

CREATE TRIGGER pgsearch_modality_codes_del AFTER DELETE ON reports_report_modalities
REFERENCING OLD TABLE AS changed
FOR EACH STATEMENT EXECUTE FUNCTION pgsearch_sync_modality_codes();

CREATE TRIGGER pgsearch_report_fields_upd AFTER UPDATE ON reports_report
REFERENCING NEW TABLE AS changed
FOR EACH STATEMENT EXECUTE FUNCTION pgsearch_sync_report_fields();
"""

DROP_TRIGGERS = """
DROP TRIGGER IF EXISTS pgsearch_group_ids_ins ON reports_report_groups;
DROP TRIGGER IF EXISTS pgsearch_group_ids_del ON reports_report_groups;
DROP TRIGGER IF EXISTS pgsearch_modality_codes_ins ON reports_report_modalities;
DROP TRIGGER IF EXISTS pgsearch_modality_codes_del ON reports_report_modalities;
DROP TRIGGER IF EXISTS pgsearch_report_fields_upd ON reports_report;
DROP FUNCTION IF EXISTS pgsearch_sync_group_ids();
DROP FUNCTION IF EXISTS pgsearch_sync_modality_codes();
DROP FUNCTION IF EXISTS pgsearch_sync_report_fields();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("pgsearch", "0003_search_projection_columns"),
        ("reports", "0013_alter_report_options"),
    ]

    operations = [
        migrations.RunSQL(SYNC_GROUP_IDS, reverse_sql=migrations.RunSQL.noop),
        migrations.RunSQL(SYNC_MODALITY_CODES, reverse_sql=migrations.RunSQL.noop),
        migrations.RunSQL(SYNC_REPORT_FIELDS, reverse_sql=migrations.RunSQL.noop),
        migrations.RunSQL(CREATE_TRIGGERS, reverse_sql=DROP_TRIGGERS),
    ]
