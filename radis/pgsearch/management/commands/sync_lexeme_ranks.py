from django.core.management.base import BaseCommand
from django.db import connection

from ...models import LexemeRank
from ...utils import lexeme_rank


class Command(BaseCommand):
    help = (
        "Install the trigger that maintains the per-lexeme rank table and backfill it "
        "for existing reports (used when HYBRID_FTS_LEXEME_RANK_INDEX is enabled). The "
        "trigger roughly doubles the write cost of (re)indexing a report, which is why "
        "it is installed here on explicit opt-in instead of by a migration. Running web "
        "processes probe for the trigger once and then cache the answer, so they pick "
        "the fast path up on their next restart."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--rebuild",
            action="store_true",
            help="Recompute every row: empty the table first, then backfill.",
        )
        parser.add_argument(
            "--remove",
            action="store_true",
            help="Drop the sync trigger and empty the table.",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=10_000,
            help="Search-index rows per backfill INSERT batch.",
        )

    def handle(self, *args, **options):
        table = LexemeRank._meta.db_table
        with connection.cursor() as cursor:
            # Inside an enclosing transaction (tests, scripted setups) freshly
            # written rows leave deferred FK trigger events pending, and CREATE
            # TRIGGER refuses to run over them; firing them now clears the
            # queue. A no-op under autocommit.
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")

            if options["remove"]:
                lexeme_rank.remove_trigger(cursor)
                cursor.execute(f"TRUNCATE {table}")
                lexeme_rank.reset_ready_cache()
                self.stdout.write("Sync trigger removed and lexeme rank table emptied.")
                return

            lexeme_rank.install_trigger(cursor)
            self.stdout.write(
                "Sync trigger installed; report writes keep lexeme ranks fresh from now on."
            )
            if options["rebuild"]:
                cursor.execute(f"TRUNCATE {table}")
                self.stdout.write("Existing lexeme ranks discarded (--rebuild).")

        def on_batch(done: int, total: int, rows: int) -> None:
            self.stdout.write(f"  backfill: {done}/{total} search index rows, {rows} rank rows")

        inserted = lexeme_rank.backfill_lexeme_ranks(options["batch_size"], on_batch=on_batch)
        lexeme_rank.reset_ready_cache()
        self.stdout.write(self.style.SUCCESS(f"Backfill complete: {inserted} rank rows inserted."))
