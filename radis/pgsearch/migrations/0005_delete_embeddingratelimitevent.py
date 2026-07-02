"""Drop the proactive rate-limit ledger. The embedding pipeline now handles
gateway rate limits reactively (429 + exponential backoff in
`radis.pgsearch.utils.rate_limiter`), so the sliding-window bookkeeping
table is no longer needed.
"""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("pgsearch", "0004_embeddingratelimitevent"),
    ]

    operations = [
        migrations.DeleteModel(name="EmbeddingRateLimitEvent"),
    ]
