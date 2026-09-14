"""Backfill the ReportSearchIndex search projection for existing rows.

atomic = False so each chunk commits on its own: at the 8M design target this
runs for roughly ten minutes, and one transaction that long would
pin an equally long-lived snapshot.

Runs after the triggers (0004) on purpose. A report edited during the backfill
is corrected by its trigger, and a chunk that later reprocesses the same row
simply rewrites the current values.

The statement below duplicates utils/projection.PROJECTION_UPDATE_SQL, the same
way bulk_upsert_report_search_indexes duplicates the tsvector logic. Migrations
must not import app code, which drifts under them. The two trigger functions in
0004 and check_search_projection.DRIFT_SQL repeat the same array_agg shape --
keep all four in sync.
"""

from django.db import migrations, transaction

CHUNK_SIZE = 50_000

# Locked as its own statement before each chunk's UPDATE, so that UPDATE's
# snapshot is taken with the locks already held. See backfill() below and
# utils/projection.sync_projection for why that matters.
LOCK_SQL = """
SELECT report_id FROM pgsearch_reportsearchindex
WHERE report_id > %s AND report_id <= %s
ORDER BY report_id
FOR UPDATE
"""

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
    """Fill the projection in report_id ranges, one transaction per chunk.

    Each chunk locks its rows before updating them. Under READ COMMITTED a
    single ``UPDATE ... FROM`` fed by a LATERAL aggregate can re-apply a stale
    aggregate to a row a concurrent trigger committed mid-statement
    (EvalPlanQual), which on ``group_ids`` would silently restore a group that
    was just removed. The documented deploy runs this with the web tier stopped
    and therefore has no concurrent writers at all, so this is belt-and-braces
    -- it costs one extra pass over each chunk, which is cheap next to the row
    rewrite the UPDATE performs anyway, and it removes the need to reason about
    whether some other writer really is quiesced.
    """
    connection = schema_editor.connection
    with connection.cursor() as cursor:
        cursor.execute("SELECT COALESCE(MAX(report_id), 0) FROM pgsearch_reportsearchindex")
        max_id = cursor.fetchone()[0]

    low = 0
    while low < max_id:
        high = low + CHUNK_SIZE
        with transaction.atomic(using=connection.alias):
            with connection.cursor() as cursor:
                cursor.execute(LOCK_SQL, [low, high])
                cursor.execute(BACKFILL_SQL, [low, high])
        low = high


class Migration(migrations.Migration):
    atomic = False

    dependencies = [("pgsearch", "0004_search_projection_triggers")]

    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
