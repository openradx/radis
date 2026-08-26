from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from radis.reports.models import Language, Report

from ...utils.bm25_utils import bm25_index_name
from ...utils.language_utils import code_to_language


class Command(BaseCommand):
    help = (
        "Create the pg_textsearch extension and one partial BM25 index per Language "
        "row (used when HYBRID_FTS_RANKING='bm25'). Languages are data, not schema, "
        "so the indexes are managed here instead of in migrations: adding a language "
        "to the corpus means re-running this command, not writing a migration. "
        "Index builds take a table lock and scale with corpus size."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Only report what would be created.",
        )

    def handle(self, *args, **options):
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM pg_available_extensions WHERE name = 'pg_textsearch'")
            if cursor.fetchone() is None:
                raise CommandError(
                    "pg_textsearch is not available in this PostgreSQL installation. "
                    "Use a postgres image that ships it (see docker/postgres/Dockerfile)."
                )
            if not options["dry_run"]:
                cursor.execute("CREATE EXTENSION IF NOT EXISTS pg_textsearch")

            # Inside an enclosing transaction (tests, scripted setups) freshly
            # written rows leave deferred FK trigger events pending, and CREATE
            # INDEX refuses to run over them; firing them now clears the queue.
            # A no-op under autocommit.
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")

            table = Report._meta.db_table
            cursor.execute("SELECT indexname FROM pg_indexes WHERE tablename = %s", [table])
            existing = {row[0] for row in cursor.fetchall()}

            for language in Language.objects.all():
                name = bm25_index_name(language.code)
                if name in existing:
                    self.stdout.write(f"{name}: exists")
                    continue
                config = code_to_language(language.code)
                if options["dry_run"]:
                    self.stdout.write(f"{name}: would create (text_config={config})")
                    continue
                self.stdout.write(f"{name}: creating (text_config={config}) ...")
                # The predicate pins the index to one language so each index
                # carries a single stemmer and its own coherent BM25 statistics.
                # DDL takes no bind parameters, so everything is inlined from
                # vetted parts: the index name sanitizes the language code, the
                # table comes from model meta, the config from our own closed
                # code_to_language mapping (asserted below), the id is an int.
                if not config.isidentifier():
                    raise CommandError(f"Unexpected text search config name: {config!r}")
                cursor.execute(
                    f'CREATE INDEX "{name}" ON "{table}" USING bm25 (body) '
                    f"WITH (text_config='{config}') WHERE language_id = {int(language.pk)}"
                )
                self.stdout.write(f"{name}: done")
