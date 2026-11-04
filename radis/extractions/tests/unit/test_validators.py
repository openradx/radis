import pytest
from django.core.exceptions import ValidationError

from radis.extractions.validators import validate_output_field_name


@pytest.mark.parametrize(
    "name",
    [
        "lung_nodule_size",
        "size",
        "Grade2",
        "model",
        "modelled_size",
        "a_model_field",
        "x1",
        "größe",
    ],
)
def test_validate_output_field_name_accepts_identifiers(name: str):
    validate_output_field_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "_size",
        "__size",
        "nodule__size",
        "size__",
        "model_config",
        "model_fields",
        "model_",
        "nodule size",
        "nodule-size",
        "1st_finding",
        "size!",
        "",
        "class",
        "None",
    ],
)
def test_validate_output_field_name_rejects_unsafe_names(name: str):
    with pytest.raises(ValidationError):
        validate_output_field_name(name)
