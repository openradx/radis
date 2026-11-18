"""The new single-table filter must select exactly what the joined one did.

_build_filter_query_legacy is the pre-change implementation kept as a reference
oracle. Delete both it and this module once the change has settled in production.
"""

from datetime import date, datetime, time, timedelta

import pytest
from adit_radis_shared.accounts.factories import GroupFactory
from django.db.models import Q
from django.test import override_settings
from django.utils import timezone

from radis.labels.factories import LabelFactory, LabelResultFactory
from radis.labels.models import LabelResult
from radis.pgsearch.models import ReportSearchIndex
from radis.pgsearch.providers import (
    _build_filter_query,
    _build_query_string,
    _fts_candidate_queryset,
    _language_configs,
)
from radis.reports.factories import LanguageFactory, ReportFactory
from radis.search.site import SearchFilters
from radis.search.utils.query_parser import QueryParser

pytestmark = pytest.mark.django_db

# A fixed calendar day used for the date-boundary tests below, well away from
# any of the corpus fixture's "N days ago" reports so it can't accidentally
# overlap with those.
BOUNDARY_DAY = date(2026, 3, 10)


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
    if filters.created_after:
        fq &= Q(report__created_at__gte=filters.created_after)
    if filters.created_before:
        fq &= Q(report__created_at__lte=filters.created_before)
    if filters.labels:
        from radis.reports.models import Report

        surfacing_report_ids = Report.objects.filter(
            label_results__label__name__in=filters.labels,
            label_results__value__in=LabelResult.SURFACING_VALUES,
        ).values("pk")
        fq &= Q(report__in=surfacing_report_ids)
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
    pneumonia = LabelFactory.create(name="pneumonia")
    LabelResultFactory.create(report=first, label=pneumonia, value=LabelResult.Value.PRESENT)

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

    return {"group_a": group_a, "group_b": group_b, "orphan": orphan, "first": first}


def _make_boundary_corpus():
    """Reports pinned to explicit local times around BOUNDARY_DAY.

    "N days ago" filters (as used by the rest of this module) never land on or
    near a report's exact date, so they cannot catch an off-by-one at the day
    boundary or a UTC-vs-local mistake in ``_local_day_start``. These reports
    are placed exactly where such mistakes would show: at local midnight
    (the inclusive lower bound), at 23:30 local (deep inside the last day, but
    the first casualty of an accidentally inclusive-upper-bound-on-exact-day
    mistake), and at local midnight of the day *after* (which must be
    excluded).

    A plain helper rather than a fixture: the non-UTC test below must create
    these reports *and* run the query while the same overridden ``TIME_ZONE``
    is active, since "local midnight" is only meaningful relative to whatever
    timezone is active at the moment ``timezone.make_aware`` runs. A fixture
    is resolved before an ``@override_settings``-decorated test body starts,
    which would pin the reports to the wrong timezone.
    """
    language_en = LanguageFactory.create(code="en")
    group = GroupFactory.create(name="Boundary")

    midnight = ReportFactory.create(
        language=language_en,
        study_datetime=timezone.make_aware(datetime.combine(BOUNDARY_DAY, time.min)),
    )
    midnight.groups.add(group)

    late = ReportFactory.create(
        language=language_en,
        study_datetime=timezone.make_aware(datetime.combine(BOUNDARY_DAY, time(23, 30))),
    )
    late.groups.add(group)

    next_midnight = ReportFactory.create(
        language=language_en,
        study_datetime=timezone.make_aware(
            datetime.combine(BOUNDARY_DAY + timedelta(days=1), time.min)
        ),
    )
    next_midnight.groups.add(group)

    return {"group": group, "midnight": midnight, "late": late, "next_midnight": next_midnight}


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
                group=c["group_a"].pk, created_after=timezone.now() - timedelta(hours=1)
            ),
            id="created-after",
        ),
        pytest.param(
            lambda c: SearchFilters(
                group=c["group_a"].pk, created_before=timezone.now() + timedelta(hours=1)
            ),
            id="created-before",
        ),
        pytest.param(
            lambda c: SearchFilters(group=c["group_a"].pk, labels=["pneumonia"]), id="labels"
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


def test_date_range_boundary_matches_legacy():
    """The half-open range must include local midnight and 23:30 local on the
    selected day, and exclude local midnight of the following day.

    This is the exact hazard the brief calls out: an off-by-one
    (``__lte=_local_day_start(till)`` instead of
    ``__lt=_local_day_start(till + 1 day)``) would silently drop the 23:30
    report here, and an inclusive-instead-of-exclusive upper bound would wrongly
    keep next_midnight -- neither is visible from "N days ago" filters alone.
    """
    boundary = _make_boundary_corpus()
    filters = SearchFilters(
        group=boundary["group"].pk,
        study_date_from=BOUNDARY_DAY,
        study_date_till=BOUNDARY_DAY,
    )

    new_ids = _ids(_build_filter_query(filters))
    legacy_ids = _ids(_build_filter_query_legacy(filters))

    assert new_ids == legacy_ids
    assert new_ids == {boundary["midnight"].pk, boundary["late"].pk}


def test_no_report_traversal_remains_in_the_candidate_query():
    """Any report__ lookup re-adds a join and the multi-second query shape."""
    import inspect

    from radis.pgsearch import providers

    source = inspect.getsource(providers)
    offending = [
        line.strip()
        for line in source.splitlines()
        if "report__" in line
        and "report__body" not in line  # hydration headline, deliberately joined
        and "report__document_id" not in line  # filter(), deliberately joined
        and not line.strip().startswith("#")
    ]

    assert offending == [], f"report__ traversals left in the hot path: {offending}"


def test_date_range_boundary_matches_legacy_in_non_utc_timezone():
    """Same boundary check under a timezone hours away from UTC.

    A mistake inside ``_local_day_start`` that resolved midnight in UTC rather
    than the active timezone would only misplace the boundary here -- under
    UTC (the default test timezone) local and UTC midnight coincide and the
    bug is invisible. The corpus is built *inside* the override so "local
    midnight" is pinned relative to the same timezone the query later uses.
    """
    with override_settings(TIME_ZONE="America/New_York"):
        boundary = _make_boundary_corpus()
        filters = SearchFilters(
            group=boundary["group"].pk,
            study_date_from=BOUNDARY_DAY,
            study_date_till=BOUNDARY_DAY,
        )

        new_ids = _ids(_build_filter_query(filters))
        legacy_ids = _ids(_build_filter_query_legacy(filters))

    assert new_ids == legacy_ids
    assert new_ids == {boundary["midnight"].pk, boundary["late"].pk}


def _candidate_queryset(filters: SearchFilters, query: str = "pneumonia"):
    """The FTS candidate queryset ``_fuse_hybrid`` actually runs.

    Built by calling the same production helpers in the same order the provider
    does, so a regression introduced anywhere along that path -- the filter, the
    tsquery match, the rank annotation, the ordering or the bound -- shows up
    here. A test that assembled its own queryset from ``_build_filter_query``
    alone could not fail, because the ``.distinct()`` calls this change removed
    never lived there.
    """
    node, _fixes = QueryParser().parse(query)
    assert node is not None
    return _fts_candidate_queryset(
        _build_filter_query(filters),
        _language_configs(filters),
        _build_query_string(node),
    )


@pytest.mark.parametrize(
    "language",
    ["en", ""],
    ids=["single-config", "every-config"],
)
def test_fts_candidate_query_is_single_table(corpus, language):
    """The candidate query must touch no table but the search index, and must
    not deduplicate.

    This is the guard against silently restoring the 8-second query shape, so
    it asserts on the real candidate queryset rather than on a re-creation of
    its filter half.

    The join check inspects the ``EXPLAIN`` plan, which is sound regardless of
    corpus size: Django only emits ``reports_report``, ``reports_language`` or
    ``reports_report_groups`` into the SQL when a lookup or an annotation
    traverses ``report__``, and no planner choice can inject them otherwise.
    Both parametrisations matter: without a language filter the match and rank
    expressions carry one branch per text-search configuration in the corpus,
    which is where a ``report__language__code`` traversal is most tempting.

    The dedup check inspects the compiled SQL text for the ``DISTINCT`` keyword
    instead of looking for a physical operator name (e.g. ``Unique``) in the
    plan -- the planner is free to implement ``DISTINCT`` as a ``Sort`` +
    ``Unique`` or as a ``HashAggregate`` depending on cost estimates, and at
    realistic corpus sizes it picks ``HashAggregate``, so a plan-based check
    would silently miss a reintroduced ``.distinct()`` at scale even though it
    would catch it on this test's tiny fixture.
    """
    from django.db import connection

    filters = SearchFilters(group=corpus["group_a"].pk, language=language)
    queryset = _candidate_queryset(filters)
    sql, params = queryset.query.sql_with_params()

    assert "DISTINCT" not in sql, sql

    with connection.cursor() as cursor:
        cursor.execute(f"EXPLAIN {sql}", params)
        plan = "\n".join(row[0] for row in cursor.fetchall())

    # "reports_report" also covers "reports_report_groups" and
    # "reports_report_modalities".
    for table in ("reports_report", "reports_language", "reports_modality"):
        assert table not in plan, plan
