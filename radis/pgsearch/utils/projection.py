"""The canonical SQL that fills the ReportSearchIndex search projection.

Triggers (migration 0004) maintain the projection when its sources change.
This module fills it when an index row is first created, which happens before
the report's groups and modalities are attached. The backfill migration
duplicates this statement in SQL, the same way bulk_upsert_report_search_indexes
already duplicates the tsvector logic -- keep them in sync.
"""

from collections.abc import Iterable

from django.db import connection

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
    """Fill the projection columns for the given reports from their sources."""
    ids = [int(report_id) for report_id in report_ids]
    if not ids:
        return
    with connection.cursor() as cursor:
        cursor.execute(PROJECTION_UPDATE_SQL, [ids])
