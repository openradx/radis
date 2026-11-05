import pytest
from django.core.exceptions import ValidationError

from radis.extractions.validators import validate_output_field_name


@pytest.mark.parametrize(
    "name",
    [
        "lung_nodule_size",
        "Pneumothorax status",
        "Size (mm)",
        "nodule-size",
        "1st finding",
        "Größe",
        "class",
        "model",
        "modelled_size",
        "a_model_field",
        "size_",
    ],
)
def test_validate_output_field_name_accepts_everyday_names(name: str):
    validate_output_field_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "_size",
        "__size",
        "model_config",
        "model_dump",
        "model_",
        "nodule.size",
        "Approx. size",
        "nodule__size",
        "size__",
    ],
)
def test_validate_output_field_name_rejects_unsafe_names(name: str):
    with pytest.raises(ValidationError):
        validate_output_field_name(name)
