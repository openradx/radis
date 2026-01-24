from __future__ import annotations

from collections.abc import Iterable

from django.conf import settings
from django.db import connection

from radis.reports.models import Report

from ..models import ReportSearchVector
from .language_utils import code_to_language


def _chunked(items: list[int], size: int) -> Iterable[list[int]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def bulk_upsert_report_search_vectors(
    report_ids: Iterable[int],
    chunk_size: int | None = None,
) -> None:
    ids = sorted({int(report_id) for report_id in report_ids if report_id is not None})
    if not ids:
        return
    resolved_chunk_size = (
        settings.PGSEARCH_BULK_INDEX_CHUNK_SIZE if chunk_size is None else chunk_size
    )

    for chunk in _chunked(ids, resolved_chunk_size):
        reports = (
            Report.objects.filter(id__in=chunk)
            .select_related("language")
            .only("id", "language__code")
        )
        config_to_ids: dict[str, list[int]] = {}
        config_cache: dict[str, str] = {}
        for report in reports:
            language_code = report.language.code
            config = config_cache.get(language_code)
            if config is None:
                config = code_to_language(language_code)
                config_cache[language_code] = config
            config_to_ids.setdefault(config, []).append(report.pk)

        for config, config_ids in config_to_ids.items():
            ReportSearchVector.objects.bulk_create(
                [ReportSearchVector(report_id=report_id) for report_id in config_ids],
                ignore_conflicts=True,
                batch_size=settings.PGSEARCH_BULK_INSERT_BATCH_SIZE,
            )

            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE pgsearch_reportsearchvector v
                    SET search_vector = to_tsvector(%s::regconfig, r.body)
                    FROM reports_report r
                    WHERE v.report_id = r.id AND r.id = ANY(%s)
                    """,
                    [config, config_ids],
                )
