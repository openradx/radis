"""Tests for ``radis.labels.site`` — the consumers of the reports event hook.

These pin the HIGH #5 ``changed_fields`` contract:

* ``None`` (or absent) re-labels (back-compat: bulk upsert + admin paths
  pass this because they don't have field-level granularity).
* A set containing ``"body"`` re-labels (the body change could affect
  answers — re-label).
* A non-empty set without ``"body"`` skips (metadata-only update;
  re-labelling would burn LLM cost for no possible benefit).
* Empty set skips (no field changed ⇒ definitely no body change).

If a future refactor breaks any of these branches, the corresponding
test fails — that's the regression guard for HIGH #5.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from radis.labels.site import handle_reports_updated
from radis.reports.models import Language, Report


def _make_report(document_id: str = "doc-1") -> Report:
    lang, _ = Language.objects.get_or_create(code="en")
    return Report.objects.create(
        document_id=document_id,
        body="Test report body",
        patient_birth_date="2000-01-01",
        patient_sex="M",
        study_datetime="2024-01-15T10:00:00Z",
        language=lang,
    )


@pytest.mark.django_db(transaction=True)
class TestHandleReportsUpdatedChangedFields:
    """The handler's ``changed_fields`` branch is what HIGH #5 turns on.

    We use ``transaction=True`` because the handler defers its real work
    inside ``transaction.on_commit``; without it the on_commit hook never
    fires during the test and the assertions on
    ``enqueue_labeling_for_reports`` would always see zero calls.
    """

    @patch("radis.labels.site.enqueue_labeling_for_reports")
    def test_none_changed_fields_re_enqueues_back_compat(self, mock_enqueue):
        """The bulk upsert and admin paths pass ``None`` because they
        don't know which fields changed. Pre-HIGH-#5 behavior is the
        baseline: re-enqueue every time. The ``None`` branch must
        preserve that.
        """
        report = _make_report()
        handle_reports_updated([report], changed_fields=None)
        mock_enqueue.assert_called_once()
        assert mock_enqueue.call_args.args[0] == [report.id]

    @patch("radis.labels.site.enqueue_labeling_for_reports")
    def test_no_changed_fields_kwarg_defaults_to_none(self, mock_enqueue):
        """If a caller invokes the handler with only the positional
        ``reports`` argument, the default ``None`` keeps back-compat.
        Equivalent to the case above; exists separately so a future
        refactor that drops the default surfaces as a test failure.
        """
        report = _make_report()
        handle_reports_updated([report])
        mock_enqueue.assert_called_once()

    @patch("radis.labels.site.enqueue_labeling_for_reports")
    def test_body_in_changed_fields_re_enqueues(self, mock_enqueue):
        """The body changed → the LLM's answer could change → re-label."""
        report = _make_report()
        handle_reports_updated([report], changed_fields={"body"})
        mock_enqueue.assert_called_once()

    @patch("radis.labels.site.enqueue_labeling_for_reports")
    def test_body_with_other_fields_still_re_enqueues(self, mock_enqueue):
        """An update that touches body AND metadata still re-labels —
        the body change is what matters, the other fields are irrelevant
        to the labelling decision.
        """
        report = _make_report()
        handle_reports_updated(
            [report], changed_fields={"body", "study_datetime", "patient_sex"}
        )
        mock_enqueue.assert_called_once()

    @patch("radis.labels.site.enqueue_labeling_for_reports")
    def test_metadata_only_changed_fields_skip_enqueue(self, mock_enqueue):
        """The HIGH #5 fix: a demographic / metadata correction must not
        trigger re-labelling. Pre-fix this would have burnt one LLM call
        per report per active set per mode for nothing.
        """
        report = _make_report()
        handle_reports_updated(
            [report], changed_fields={"study_datetime", "patient_sex"}
        )
        assert not mock_enqueue.called

    @patch("radis.labels.site.enqueue_labeling_for_reports")
    def test_empty_changed_fields_skip_enqueue(self, mock_enqueue):
        """Empty set ⇒ no fields changed ⇒ definitely no body change.
        Practically the API shouldn't fire the hook for empty updates,
        but the branch must do the right thing if it ever does.
        """
        report = _make_report()
        handle_reports_updated([report], changed_fields=set())
        assert not mock_enqueue.called

    @patch("radis.labels.site.enqueue_labeling_for_reports")
    def test_skips_when_no_reports_even_with_body_changed(self, mock_enqueue):
        """Edge: empty ``reports`` iterable. The body-in-changed-fields
        branch passes through, but the empty-list guard further down
        still short-circuits without enqueueing.
        """
        handle_reports_updated([], changed_fields={"body"})
        assert not mock_enqueue.called

    @patch("radis.labels.site.enqueue_labeling_for_reports")
    def test_multiple_reports_all_re_enqueue_when_body_changed(self, mock_enqueue):
        """The skip / re-enqueue decision is per-call, not per-report.
        All reports in the batch share the same ``changed_fields``
        (because the API perform_update path serves one record at a
        time, but the contract supports lists for forward compatibility).
        """
        r1 = _make_report("doc-a")
        r2 = _make_report("doc-b")
        handle_reports_updated([r1, r2], changed_fields={"body"})
        mock_enqueue.assert_called_once()
        assert set(mock_enqueue.call_args.args[0]) == {r1.id, r2.id}
