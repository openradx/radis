"""Backfill the ReportSearchIndex search projection for existing rows.

atomic = False so each chunk commits on its own: at the 8M design target this
runs for about nine and a half minutes, and one transaction that long would
pin an equally long-lived snapshot.

Runs after the triggers (0004) on purpose. A report edited during the backfill
is corrected by its trigger, and a chunk that later reprocesses the same row
simply rewrites the current values.

The statement below duplicates utils/projection.PROJECTION_UPDATE_SQL, the same
way bulk_upsert_report_search_indexes duplicates the tsvector logic. Migrations
must not import app code, which drifts under them.
"""

from django.db import migrations

CHUNK_SIZE = 50_000

BACKFILL_SQL = """
UPDATE pgsearch_reportsearchindex rsi
SET group_ids = COALESCE(g.ids, '{}'),
    modality_codes = COALESCE(m.codes, '{}'),
    language_code = l.code,
    patient_sex = r.patient_sex,
    patient_age = r.patient_age,
    patient_id = r.patient_id,
    study_datetime = r.study_datetime,
    study_description = r.study_description,
    report_created_at = r.created_at,
    report_updated_at = r.updated_at
FROM reports_report r
LEFT JOIN reports_language l ON l.id = r.language_id
LEFT JOIN LATERAL (
    SELECT array_agg(rg.group_id ORDER BY rg.group_id) AS ids
      FROM reports_report_groups rg WHERE rg.report_id = r.id
) g ON true
LEFT JOIN LATERAL (
    SELECT array_agg(mo.code ORDER BY mo.code) AS codes
      FROM reports_report_modalities rm
      JOIN reports_modality mo ON mo.id = rm.modality_id
     WHERE rm.report_id = r.id
) m ON true
WHERE rsi.report_id = r.id AND rsi.report_id > %s AND rsi.report_id <= %s
"""


def backfill(apps, schema_editor):
    connection = schema_editor.connection
    with connection.cursor() as cursor:
        cursor.execute("SELECT COALESCE(MAX(report_id), 0) FROM pgsearch_reportsearchindex")
        max_id = cursor.fetchone()[0]

    low = 0
    while low < max_id:
        high = low + CHUNK_SIZE
        with connection.cursor() as cursor:
            cursor.execute(BACKFILL_SQL, [low, high])
        low = high


class Migration(migrations.Migration):
    atomic = False

    dependencies = [("pgsearch", "0004_search_projection_triggers")]

    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
