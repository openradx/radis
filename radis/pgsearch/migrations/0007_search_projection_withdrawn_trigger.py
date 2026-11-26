"""Extend the report-fields projection trigger with the withdrawn flag.

Only the function body changes. The trigger declaration from 0003
(pgsearch_report_fields_upd, AFTER UPDATE ON reports_report, statement level
with a transition table, no column list) already fires on every report update,
so replacing the function is the whole change. The reverse operation restores
the 0003 body, leaving a rollback exactly as 0003 defined it.

No backfill follows: reports.withdrawn_at arrives in the same release
(reports 0014), so no report is withdrawn yet and the column default is
already correct for every row.
"""

from django.db import migrations

# The scalar columns of the projection, as in utils/projection.PROJECTION_UPDATE_SQL
# and check_search_projection.DRIFT_SQL (which also cover the two array columns).
# A new scalar needs a migration that replaces this function again, plus the same
# column in those two statements: the drift query cannot detect a column it does
# not compare.
SYNC_REPORT_FIELDS_WITH_WITHDRAWN = """
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
        report_updated_at = c.updated_at,
        withdrawn = (c.withdrawn_at IS NOT NULL)
    FROM changed c
    LEFT JOIN reports_language l ON l.id = c.language_id
    WHERE rsi.report_id = c.id
      AND (rsi.language_code, rsi.patient_sex, rsi.patient_age, rsi.patient_id,
           rsi.study_datetime, rsi.study_description, rsi.report_created_at,
           rsi.report_updated_at, rsi.withdrawn)
          IS DISTINCT FROM
          (l.code, c.patient_sex, c.patient_age, c.patient_id,
           c.study_datetime, c.study_description, c.created_at, c.updated_at,
           (c.withdrawn_at IS NOT NULL));
    RETURN NULL;
END $$;
"""

SYNC_REPORT_FIELDS_WITHOUT_WITHDRAWN = """
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


class Migration(migrations.Migration):
    dependencies = [
        ("pgsearch", "0006_reportsearchindex_withdrawn"),
        ("reports", "0014_report_withdrawal_fields"),
    ]

    operations = [
        migrations.RunSQL(
            SYNC_REPORT_FIELDS_WITH_WITHDRAWN,
            SYNC_REPORT_FIELDS_WITHOUT_WITHDRAWN,
        ),
    ]
