"""Verify the ReportSearchIndex search projection against its sources."""

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

# Same aggregation as utils/projection.PROJECTION_UPDATE_SQL, the 0005 backfill
# and the two trigger functions in migration 0004 -- four copies, keep them in
# sync. This one is the detector: if it ever drifts in the same direction as a
# writer, it silently agrees with the bug and reports a healthy projection.
DRIFT_SQL = """
SELECT
    count(*) FILTER (WHERE rsi.group_ids IS DISTINCT FROM COALESCE(g.ids, '{}'))
        AS group_ids,
    count(*) FILTER (WHERE rsi.modality_codes IS DISTINCT FROM COALESCE(m.codes, '{}'))
        AS modality_codes,
    count(*) FILTER (WHERE rsi.language_code IS DISTINCT FROM l.code)
        AS language_code,
    count(*) FILTER (WHERE rsi.patient_sex IS DISTINCT FROM r.patient_sex)
        AS patient_sex,
    count(*) FILTER (WHERE rsi.patient_age IS DISTINCT FROM r.patient_age)
        AS patient_age,
    count(*) FILTER (WHERE rsi.patient_id IS DISTINCT FROM r.patient_id)
        AS patient_id,
    count(*) FILTER (WHERE rsi.study_datetime IS DISTINCT FROM r.study_datetime)
        AS study_datetime,
    count(*) FILTER (WHERE rsi.study_description IS DISTINCT FROM r.study_description)
        AS study_description,
    count(*) FILTER (WHERE rsi.report_created_at IS DISTINCT FROM r.created_at)
        AS report_created_at,
    count(*) FILTER (WHERE rsi.report_updated_at IS DISTINCT FROM r.updated_at)
        AS report_updated_at
FROM pgsearch_reportsearchindex rsi
JOIN reports_report r ON r.id = rsi.report_id
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
"""


# Informational, not drift: an index row is created by a signal or by the bulk
# indexing path, so a report can legitimately be waiting for one. The number is
# only meaningful over time -- a few hundred that drain are normal, tens of
# thousands that never move mean indexing is stuck.
MISSING_INDEX_ROWS_SQL = """
SELECT count(*)
  FROM reports_report r
  LEFT JOIN pgsearch_reportsearchindex rsi ON rsi.report_id = r.id
 WHERE rsi.report_id IS NULL
"""


class Command(BaseCommand):
    help = "Verify the ReportSearchIndex search projection against its sources."

    def handle(self, *args, **options):
        with connection.cursor() as cursor:
            cursor.execute(DRIFT_SQL)
            columns = [column.name for column in cursor.description]
            counts = dict(zip(columns, cursor.fetchone(), strict=True))

            cursor.execute(MISSING_INDEX_ROWS_SQL)
            missing_index_rows = cursor.fetchone()[0]

        self.stdout.write(
            f"Reports without a search index row: {missing_index_rows} "
            "(indexing is deferred, so a number that keeps falling is normal)"
        )

        drifted = {name: count for name, count in counts.items() if count}
        if drifted:
            details = ", ".join(f"{name}: {count}" for name, count in sorted(drifted.items()))
            raise CommandError(f"Search projection drift detected -- {details}")

        self.stdout.write(self.style.SUCCESS("Search projection matches its sources."))
