"""The new single-table filter must select exactly what the joined one did.

_build_filter_query_legacy is the pre-change implementation kept as a reference
oracle. Delete both it and this module once the change has settled in production.
"""

from datetime import date, timedelta

import pytest
from adit_radis_shared.accounts.factories import GroupFactory
from django.db.models import Q
from django.utils import timezone

from radis.pgsearch.models import ReportSearchIndex
from radis.pgsearch.providers import _build_filter_query
from radis.reports.factories import LanguageFactory, ReportFactory
from radis.search.site import SearchFilters

pytestmark = pytest.mark.django_db


def _build_filter_query_legacy(filters: SearchFilters) -> Q:
    """The joined implementation this change replaces."""
    fq = Q(report__groups=filters.group)
    if filters.patient_sex:
        fq &= Q(report__patient_sex=filters.patient_sex)
    if filters.language:
        fq &= Q(report__language__code=filters.language)
    if filters.modalities:
        fq &= Q(report__modalities__code__in=filters.modalities)
    if filters.study_date_from:
        fq &= Q(report__study_datetime__date__gte=filters.study_date_from)
    if filters.study_date_till:
        fq &= Q(report__study_datetime__date__lte=filters.study_date_till)
    if filters.study_description:
        fq &= Q(report__study_description__icontains=filters.study_description)
    if filters.patient_age_from is not None:
        fq &= Q(report__patient_age__gte=filters.patient_age_from)
    if filters.patient_age_till is not None:
        fq &= Q(report__patient_age__lte=filters.patient_age_till)
    if filters.patient_id:
        fq &= Q(report__patient_id=filters.patient_id)
    if filters.updated_after:
        fq &= Q(report__updated_at__gte=filters.updated_after)
    return fq


def _ids(fq: Q) -> set[int]:
    return set(ReportSearchIndex.objects.filter(fq).distinct().values_list("report_id", flat=True))


@pytest.fixture
def corpus():
    language_en = LanguageFactory.create(code="en")
    language_de = LanguageFactory.create(code="de")
    group_a = GroupFactory.create(name="A")
    group_b = GroupFactory.create(name="B")
    now = timezone.now()

    first = ReportFactory.create(
        language=language_en,
        patient_sex="M",
        patient_id="P1",
        study_description="CT Thorax",
        study_datetime=now - timedelta(days=10),
        modalities=["CT"],
    )
    first.groups.add(group_a)

    second = ReportFactory.create(
        language=language_de,
        patient_sex="F",
        patient_id="P2",
        study_description="MR Head",
        study_datetime=now - timedelta(days=400),
        modalities=["MR", "CT"],
    )
    second.groups.add(group_a, group_b)

    orphan = ReportFactory.create(language=language_en, patient_sex="M", patient_id="P3")

    return {"group_a": group_a, "group_b": group_b, "orphan": orphan}


@pytest.mark.parametrize(
    "make_filters",
    [
        pytest.param(lambda c: SearchFilters(group=c["group_a"].pk), id="group-only"),
        pytest.param(lambda c: SearchFilters(group=c["group_b"].pk), id="other-group"),
        pytest.param(lambda c: SearchFilters(group=c["group_a"].pk, language="en"), id="language"),
        pytest.param(
            lambda c: SearchFilters(group=c["group_a"].pk, modalities=["CT"]), id="modality"
        ),
        pytest.param(
            lambda c: SearchFilters(group=c["group_a"].pk, modalities=["CT", "MR"]),
            id="modalities-multi",
        ),
        pytest.param(lambda c: SearchFilters(group=c["group_a"].pk, patient_sex="M"), id="sex"),
        pytest.param(
            lambda c: SearchFilters(group=c["group_a"].pk, patient_id="P1"), id="patient-id"
        ),
        pytest.param(
            lambda c: SearchFilters(group=c["group_a"].pk, study_description="thorax"),
            id="description-icontains",
        ),
        pytest.param(
            lambda c: SearchFilters(
                group=c["group_a"].pk, study_date_from=date.today() - timedelta(days=30)
            ),
            id="date-from",
        ),
        pytest.param(
            lambda c: SearchFilters(
                group=c["group_a"].pk, study_date_till=date.today() - timedelta(days=5)
            ),
            id="date-till",
        ),
        pytest.param(
            lambda c: SearchFilters(
                group=c["group_a"].pk,
                study_date_from=date.today() - timedelta(days=30),
                study_date_till=date.today(),
            ),
            id="date-range",
        ),
        pytest.param(
            lambda c: SearchFilters(group=c["group_a"].pk, patient_age_from=0), id="age-from"
        ),
        pytest.param(
            lambda c: SearchFilters(group=c["group_a"].pk, patient_age_till=200), id="age-till"
        ),
        pytest.param(
            lambda c: SearchFilters(
                group=c["group_a"].pk, updated_after=timezone.now() - timedelta(hours=1)
            ),
            id="updated-after",
        ),
    ],
)
def test_new_filter_matches_the_legacy_one(corpus, make_filters):
    filters = make_filters(corpus)

    assert _ids(_build_filter_query(filters)) == _ids(_build_filter_query_legacy(filters))


def test_group_none_is_fail_closed(corpus):
    """group=None must match only reports in no group at all -- never everything."""
    result = _ids(_build_filter_query(SearchFilters(group=None)))

    assert result == {corpus["orphan"].pk}


def test_no_duplicates_without_distinct(corpus):
    """A report matching on two modalities must appear once, which is what the
    removed .distinct() used to guarantee."""
    filters = SearchFilters(group=corpus["group_a"].pk, modalities=["CT", "MR"])

    rows = list(
        ReportSearchIndex.objects.filter(_build_filter_query(filters)).values_list(
            "report_id", flat=True
        )
    )

    assert len(rows) == len(set(rows))
