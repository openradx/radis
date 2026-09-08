"""The canonical SQL that fills the ReportSearchIndex search projection.

Triggers (migration 0004) maintain the projection when its sources change.
This module fills it when an index row is first created, which happens before
the report's groups and modalities are attached. The backfill migration
duplicates this statement in SQL, the same way bulk_upsert_report_search_indexes
already duplicates the tsvector logic -- keep them in sync. The two trigger
functions in migration 0004 and check_search_projection.DRIFT_SQL repeat the
same array_agg shape and belong to that set as well.
"""

from collections.abc import Iterable

from django.db import connection, transaction

# Taken as its own statement before the UPDATE below, so the UPDATE's snapshot
# is the one taken with these locks already held. See sync_projection().
PROJECTION_LOCK_SQL = """
SELECT report_id FROM pgsearch_reportsearchindex
WHERE report_id = ANY(%s)
ORDER BY report_id
FOR UPDATE
"""

PROJECTION_UPDATE_SQL = """
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
WHERE rsi.report_id = r.id AND rsi.report_id = ANY(%s)
"""


def sync_projection(report_ids: Iterable[int]) -> None:
    """Fill the projection columns for the given reports from their sources.

    The row locks are taken first, in their own statement, to close a
    lost-update race. Under READ COMMITTED a single ``UPDATE ... FROM`` whose
    SET values come from a LATERAL aggregate can re-apply a *stale* aggregate:
    if a trigger-driven update to a targeted row commits after this statement's
    snapshot but before the statement reaches that row, PostgreSQL's
    EvalPlanQual re-check re-evaluates the row against the new version and
    writes the value computed from the old snapshot. On ``group_ids`` that
    silently reverts a group removal -- a report readable by a group it was
    deliberately removed from, which is the fail-open direction.

    Each statement in READ COMMITTED takes its own snapshot, so locking first
    splits the race in two: a trigger write that would have committed later now
    blocks on these locks until this transaction ends, and one that committed
    earlier is already visible to the UPDATE's snapshot. ``ORDER BY report_id``
    makes the lock order deterministic so concurrent callers queue rather than
    deadlock.
    """
    ids = [int(report_id) for report_id in report_ids]
    if not ids:
        return
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(PROJECTION_LOCK_SQL, [ids])
            cursor.execute(PROJECTION_UPDATE_SQL, [ids])
