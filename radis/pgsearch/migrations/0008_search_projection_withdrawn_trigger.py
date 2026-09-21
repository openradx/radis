"""Extend the report-fields projection trigger with the withdrawn flag.

Only the function body changes. The trigger declaration from 0004
(pgsearch_report_fields_upd, AFTER UPDATE ON reports_report, statement level
with a transition table, no column list) already fires on every report update,
so replacing the function is the whole change. The reverse operation restores
the 0004 body, leaving a rollback exactly as 0004 defined it.
"""

from django.db import migrations

# Same column list as utils/projection.PROJECTION_UPDATE_SQL and
# check_search_projection.DRIFT_SQL -- keep them in sync. The drift query is
# the detector: a change made here and not there is a bug that reports itself
# as healthy.
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
        ("pgsearch", "0007_reportsearchindex_withdrawn"),
        ("reports", "0014_report_withdrawal_fields"),
    ]

    operations = [
        migrations.RunSQL(
            SYNC_REPORT_FIELDS_WITH_WITHDRAWN,
            SYNC_REPORT_FIELDS_WITHOUT_WITHDRAWN,
        ),
    ]
