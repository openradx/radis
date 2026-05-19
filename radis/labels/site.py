from __future__ import annotations

from typing import Iterable

from django.db import transaction

from radis.reports.models import Report

from .tasks import enqueue_labeling_for_reports


# Field on Report whose value the LLM actually sees. If this field is not
# in the ``changed_fields`` set, an update couldn't have affected the
# labelling output and we skip re-labelling. Other fields (study_datetime,
# patient_sex, etc.) are pure metadata and re-labelling on them is waste.
_BODY_FIELD = "body"


def handle_reports_created(reports: Iterable[Report]) -> None:
    report_ids = [int(getattr(report, "id")) for report in reports]
    if not report_ids:
        return

    def on_commit() -> None:
        enqueue_labeling_for_reports(report_ids)

    transaction.on_commit(on_commit)


def handle_reports_updated(
    reports: Iterable[Report], changed_fields: set[str] | None = None
) -> None:
    """React to a report update by enqueueing labelling — but only if the
    update could have affected the LLM's answers.

    ``changed_fields`` semantics (HIGH #5 fix):

    * ``None`` — the caller doesn't know which fields changed. We
      conservatively assume the body could have changed and re-enqueue.
      This is what the bulk-upsert API and the admin pass, since neither
      has clean access to the pre-save state. It matches the pre-HIGH-#5
      behavior and is the safe default.
    * Non-empty ``set[str]`` — the precise set of fields the caller asked
      to update. If ``body`` is absent, the update was metadata-only
      (demographics correction, ``study_datetime`` fix, etc.) and we
      skip re-labelling entirely. This is the cost win the HIGH #5 fix
      delivers on the single-report API path.

    Notes:

    * Empty set is treated like a metadata-only update (no fields ⇒ no
      body change ⇒ skip). Practically this branch shouldn't fire — the
      API doesn't fire the hook for empty updates — but it's the right
      behavior if it ever does.
    * We do not look at the prior body value. If the caller said
      ``"body"`` is in the update but the new body happens to equal the
      old body, we still re-label. This is fine: that case is rare and
      the cost is one LLM call rather than thousands.
    """
    if changed_fields is not None and _BODY_FIELD not in changed_fields:
        return

    report_ids = [int(getattr(report, "id")) for report in reports]
    if not report_ids:
        return

    def on_commit() -> None:
        enqueue_labeling_for_reports(report_ids)

    transaction.on_commit(on_commit)
