"""Direct unit tests for ``ReportSerializer``.

These complement the API-level contract tests in ``test_api.py`` by exercising
the serializer in isolation: the wire-format reshaping done in
``to_internal_value`` / ``to_representation``, the unknown-field rejection in
``validate``, the ``skip_document_id_unique`` context switch, and the
type-validation error branches.

The serializer's ``create``/``update`` touch the DB, so those tests are marked
``django_db``; the pure (de)serialization tests do not need a database.
"""

from datetime import date, datetime, timezone

import pytest
from adit_radis_shared.accounts.factories import GroupFactory
from rest_framework import validators
from rest_framework.exceptions import ValidationError

from radis.reports.api.serializers import ReportSerializer
from radis.reports.factories import LanguageFactory, ReportFactory
from radis.reports.models import Language, Report


def wire_payload(document_id: str = "doc-ser-0001", **overrides) -> dict:
    """A valid create payload in the flat wire format the serializer accepts.

    ``language`` is a string, ``metadata`` a flat dict, ``modalities`` a list of
    codes, ``groups`` a list of Group PKs.
    """
    group = overrides.pop("group", None)
    group_pk = group.pk if group is not None else 1
    payload = {
        "document_id": document_id,
        "language": "en",
        "groups": [group_pk],
        "pacs_aet": "synapse",
        "pacs_name": "Synapse",
        "pacs_link": "http://synapse.example/1",
        "patient_id": "1234578",
        "patient_birth_date": date(1976, 5, 23).isoformat(),
        "patient_sex": "M",
        "study_description": "CT of the Thorax",
        "study_datetime": datetime(2000, 8, 10, 11, 37, tzinfo=timezone.utc).isoformat(),
        "study_instance_uid": "34343-34343-34343",
        "accession_number": "345348389",
        "modalities": ["CT", "PT"],
        "metadata": {"series_instance_uid": "1.2.3", "sop_instance_uid": "4.5.6"},
        "body": "This is the report",
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------- #
# to_internal_value: wire format -> nested serializer format
# --------------------------------------------------------------------------- #


@pytest.mark.django_db
def test_to_internal_value_reshapes_language_metadata_modalities():
    group = GroupFactory.create()
    serializer = ReportSerializer(data=wire_payload(group=group))

    assert serializer.is_valid(), serializer.errors
    vd = serializer.validated_data

    # language string -> {"code": ...}
    assert vd["language"] == {"code": "en"}
    # metadata dict -> list of {key, value}
    assert sorted(vd["metadata"], key=lambda m: m["key"]) == [
        {"key": "series_instance_uid", "value": "1.2.3"},
        {"key": "sop_instance_uid", "value": "4.5.6"},
    ]
    # modalities list[str] -> list of {"code": ...}
    assert [m["code"] for m in vd["modalities"]] == ["CT", "PT"]


# FIXED: the three type-guard branches in ``to_internal_value`` now raise a
# *dict-shaped* ``ValidationError`` (e.g. ``{"language": "Invalid language
# type."}``). Previously they raised ``ValidationError("Invalid <x> type.")``
# with a bare string detail, which a nested ``ModelSerializer`` tried to fold
# into a field-keyed dict, raising an uncaught ``ValueError`` (HTTP 500) instead
# of the intended clean ``ValidationError`` -> 400. These tests now assert the
# correct 400 behavior.


@pytest.mark.django_db
def test_to_internal_value_rejects_non_string_language():
    group = GroupFactory.create()
    serializer = ReportSerializer(data=wire_payload(group=group, language=["en"]))

    with pytest.raises(ValidationError) as exc:
        serializer.is_valid(raise_exception=True)
    assert "Invalid language type." in str(exc.value)


@pytest.mark.django_db
def test_to_internal_value_rejects_non_dict_metadata():
    group = GroupFactory.create()
    serializer = ReportSerializer(
        data=wire_payload(group=group, metadata=[["k", "v"]])
    )

    with pytest.raises(ValidationError) as exc:
        serializer.is_valid(raise_exception=True)
    assert "Invalid metadata type." in str(exc.value)


@pytest.mark.django_db
def test_to_internal_value_rejects_non_list_modalities():
    group = GroupFactory.create()
    serializer = ReportSerializer(
        data=wire_payload(group=group, modalities={"code": "CT"})
    )

    with pytest.raises(ValidationError) as exc:
        serializer.is_valid(raise_exception=True)
    assert "Invalid modalities type." in str(exc.value)


# --------------------------------------------------------------------------- #
# to_representation: model instance -> flat wire format (round trip)
# --------------------------------------------------------------------------- #


@pytest.mark.django_db
def test_to_representation_collapses_to_wire_format():
    LanguageFactory.create(code="en")
    report = ReportFactory.create(
        document_id="doc-repr-unit",
        language=Language.objects.get(code="en"),
        modalities=["CT", "MR"],
    )

    data = ReportSerializer(report).data

    assert data["language"] == "en"
    assert isinstance(data["metadata"], dict)
    assert sorted(data["modalities"]) == ["CT", "MR"]
    assert data["document_id"] == "doc-repr-unit"


@pytest.mark.django_db
def test_create_then_representation_round_trips():
    """A payload run through create() and back through to_representation()
    yields the same flat language/metadata/modalities shapes.

    Note: ``to_internal_value`` mutates the *input* dict in place (it rewrites
    ``data["language"]`` to ``{"code": ...}`` etc.), so we snapshot the original
    wire values before validating and compare the representation against those.
    """
    group = GroupFactory.create()
    payload = wire_payload(document_id="doc-roundtrip", group=group)
    expected = {
        "language": payload["language"],
        "metadata": dict(payload["metadata"]),
        "modalities": list(payload["modalities"]),
        "document_id": payload["document_id"],
    }
    serializer = ReportSerializer(data=payload)
    assert serializer.is_valid(), serializer.errors

    report = serializer.save()
    out = ReportSerializer(report).data

    assert out["language"] == expected["language"]
    assert out["metadata"] == expected["metadata"]
    assert sorted(out["modalities"]) == sorted(expected["modalities"])
    assert out["document_id"] == expected["document_id"]
    # Document the in-place mutation side effect explicitly.
    assert payload["language"] == {"code": "en"}


# --------------------------------------------------------------------------- #
# validate(): unknown-field rejection
# --------------------------------------------------------------------------- #


@pytest.mark.django_db
def test_validate_rejects_unknown_field():
    group = GroupFactory.create()
    payload = wire_payload(group=group)
    payload["totally_unknown"] = "x"
    serializer = ReportSerializer(data=payload)

    with pytest.raises(ValidationError) as exc:
        serializer.is_valid(raise_exception=True)
    assert "unknown fields" in str(exc.value)
    assert "totally_unknown" in str(exc.value)


@pytest.mark.django_db
def test_validate_accepts_exactly_known_fields():
    group = GroupFactory.create()
    serializer = ReportSerializer(data=wire_payload(group=group))
    assert serializer.is_valid(), serializer.errors


# --------------------------------------------------------------------------- #
# Required-field validation branches
# --------------------------------------------------------------------------- #


@pytest.mark.django_db
def test_missing_required_body_is_invalid():
    group = GroupFactory.create()
    payload = wire_payload(group=group)
    del payload["body"]
    serializer = ReportSerializer(data=payload)

    assert not serializer.is_valid()
    assert "body" in serializer.errors


@pytest.mark.django_db
def test_invalid_patient_sex_is_rejected():
    group = GroupFactory.create()
    serializer = ReportSerializer(data=wire_payload(group=group, patient_sex="X"))

    assert not serializer.is_valid()
    assert "patient_sex" in serializer.errors


# --------------------------------------------------------------------------- #
# document_id uniqueness validator and the skip_document_id_unique switch
# --------------------------------------------------------------------------- #


@pytest.mark.django_db
def test_duplicate_document_id_rejected_by_unique_validator():
    LanguageFactory.create(code="en")
    ReportFactory.create(document_id="dup-doc", modalities=["CT"])
    group = GroupFactory.create()

    serializer = ReportSerializer(data=wire_payload(document_id="dup-doc", group=group))

    assert not serializer.is_valid()
    assert "document_id" in serializer.errors


@pytest.mark.django_db
def test_skip_document_id_unique_context_strips_unique_validator():
    """With ``skip_document_id_unique`` in context the unique validator on
    ``document_id`` is removed, so a colliding id validates (the bulk path relies
    on this and resolves the collision itself)."""
    LanguageFactory.create(code="en")
    ReportFactory.create(document_id="dup-doc-skip", modalities=["CT"])
    group = GroupFactory.create()

    serializer = ReportSerializer(
        data=wire_payload(document_id="dup-doc-skip", group=group),
        context={"skip_document_id_unique": True},
    )

    assert serializer.is_valid(), serializer.errors
    # the unique validator is no longer attached to the field
    assert not any(
        isinstance(v, validators.UniqueValidator)
        for v in serializer.fields["document_id"].validators
    )


# --------------------------------------------------------------------------- #
# update(): existing report mutation + metadata/modality replacement
# --------------------------------------------------------------------------- #


@pytest.mark.django_db
def test_update_replaces_metadata_and_modalities():
    LanguageFactory.create(code="en")
    group = GroupFactory.create()
    report = ReportFactory.create(
        document_id="doc-upd-unit",
        language=Language.objects.get(code="en"),
        modalities=["CT"],
    )

    updated = wire_payload(document_id="doc-upd-unit", group=group)
    updated["body"] = "new body"
    updated["modalities"] = ["US"]
    updated["metadata"] = {"only": "one"}

    serializer = ReportSerializer(report, data=updated)
    assert serializer.is_valid(), serializer.errors
    result = serializer.save()

    result.refresh_from_db()
    assert result.body == "new body"
    assert result.modality_codes == ["US"]
    assert {m.key: m.value for m in result.metadata.all()} == {"only": "one"}


@pytest.mark.django_db
def test_update_with_new_language_creates_it():
    """``update()`` uses ``Language.objects.get_or_create(**language)`` (matching
    ``create()``), so PUTting a brand-new language code onto an existing report
    creates the ``Language`` and assigns it instead of raising
    ``Language.DoesNotExist`` (which previously surfaced as an unhandled 500).
    """
    LanguageFactory.create(code="en")
    group = GroupFactory.create()
    report = ReportFactory.create(
        document_id="doc-upd-lang",
        language=Language.objects.get(code="en"),
        modalities=["CT"],
    )
    assert not Language.objects.filter(code="zz").exists()

    updated = wire_payload(document_id="doc-upd-lang", group=group, language="zz")
    serializer = ReportSerializer(report, data=updated)
    assert serializer.is_valid(), serializer.errors

    result = serializer.save()

    result.refresh_from_db()
    assert result.language.code == "zz"
    assert Language.objects.filter(code="zz").exists()


@pytest.mark.django_db
def test_many_true_serializes_list_of_reports():
    LanguageFactory.create(code="en")
    ReportFactory.create(document_id="m-1", modalities=["CT"])
    ReportFactory.create(document_id="m-2", modalities=["MR"])

    data = ReportSerializer(Report.objects.order_by("document_id"), many=True).data

    ids = {row["document_id"] for row in data}
    assert {"m-1", "m-2"} <= ids
    for row in data:
        assert isinstance(row["language"], str)
        assert isinstance(row["modalities"], list)
