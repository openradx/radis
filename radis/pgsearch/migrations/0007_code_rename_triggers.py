"""Close two projection writers the 0004 triggers do not see: code renames.

0004 syncs membership changes and report-row updates, but the mirrors store
the *code strings* of language and modalities, and nothing watched the code
columns themselves. Renaming a language silently strips its whole corpus from
search: every index row keeps mirroring the old code, so the language filter
and the per-config tsquery match both stop matching, while the drift checker
is the only thing that would notice. 0004's docstring lists three escaping
writers; these were the unlisted fourth and fifth.

Row-level rather than 0004's statement-level shape, deliberately: PostgreSQL
does not allow transition tables on triggers with column lists ("transition
tables cannot be specified for triggers with column lists"), and unlike the
bulk membership writes 0004 handles, a rename statement touches one dimension
row -- FOR EACH ROW with a WHEN guard fires exactly once per actual code
change and costs nothing on these tiny tables. The modality function repeats
the array_agg aggregation of utils/projection.PROJECTION_UPDATE_SQL, the 0005
backfill, 0004's membership functions and check_search_projection.DRIFT_SQL --
keep them all in sync.

Cost note: renaming a language rewrites the projection row of every report in
that language, and on an embedded corpus each rewrite also inserts into the
HNSW index (~143 ms/row measured). Renames are assumed rare and small; a
corpus-wide rename belongs in a maintenance window.
"""

from django.db import migrations

SYNC_LANGUAGE_CODE = """
CREATE OR REPLACE FUNCTION pgsearch_sync_language_code() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    UPDATE pgsearch_reportsearchindex rsi
    SET language_code = NEW.code
    FROM reports_report r
    WHERE r.language_id = NEW.id
      AND rsi.report_id = r.id
      AND rsi.language_code IS DISTINCT FROM NEW.code;
    RETURN NULL;
END $$;
"""

SYNC_MODALITY_CODE_RENAME = """
CREATE OR REPLACE FUNCTION pgsearch_sync_modality_code_rename() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    UPDATE pgsearch_reportsearchindex rsi
    SET modality_codes = full_agg.codes
    FROM (
        SELECT rm.report_id,
               array_agg(m.code ORDER BY m.code) AS codes
          FROM reports_report_modalities rm
          JOIN reports_modality m ON m.id = rm.modality_id
         WHERE rm.report_id IN (
             SELECT rm2.report_id FROM reports_report_modalities rm2
              WHERE rm2.modality_id = NEW.id)
         GROUP BY rm.report_id
    ) full_agg
    WHERE rsi.report_id = full_agg.report_id
      AND rsi.modality_codes IS DISTINCT FROM full_agg.codes;
    RETURN NULL;
END $$;
"""

CREATE_TRIGGERS = """
CREATE TRIGGER pgsearch_language_code_upd
AFTER UPDATE OF code ON reports_language
FOR EACH ROW WHEN (OLD.code IS DISTINCT FROM NEW.code)
EXECUTE FUNCTION pgsearch_sync_language_code();

CREATE TRIGGER pgsearch_modality_code_upd
AFTER UPDATE OF code ON reports_modality
FOR EACH ROW WHEN (OLD.code IS DISTINCT FROM NEW.code)
EXECUTE FUNCTION pgsearch_sync_modality_code_rename();
"""

DROP_TRIGGERS = """
DROP TRIGGER IF EXISTS pgsearch_language_code_upd ON reports_language;
DROP TRIGGER IF EXISTS pgsearch_modality_code_upd ON reports_modality;
DROP FUNCTION IF EXISTS pgsearch_sync_language_code();
DROP FUNCTION IF EXISTS pgsearch_sync_modality_code_rename();
"""


class Migration(migrations.Migration):
    dependencies = [("pgsearch", "0006_search_projection_indexes")]

    operations = [
        migrations.RunSQL(
            SYNC_LANGUAGE_CODE + SYNC_MODALITY_CODE_RENAME + CREATE_TRIGGERS,
            reverse_sql=DROP_TRIGGERS,
        ),
    ]
