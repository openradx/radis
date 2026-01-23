"""
Reusable form field factories for RADIS forms.

This module provides factory functions for commonly used form fields
to reduce duplication across the codebase.
"""

from typing import Literal, overload

from django import forms

from radis.core.constants import AGE_STEP, LANGUAGE_LABELS, MAX_AGE, MIN_AGE
from radis.reports.models import Language, Modality


@overload
def create_language_field(
    required: bool = False,
    empty_label: str | None = None,
    use_pk: Literal[True] = True,
) -> forms.ModelChoiceField: ...


@overload
def create_language_field(
    required: bool = False,
    empty_label: str | None = None,
    use_pk: Literal[False] = False,
) -> forms.ChoiceField: ...


def create_language_field(
    required: bool = False,
    empty_label: str | None = None,
    use_pk: bool = True,
) -> forms.ModelChoiceField | forms.ChoiceField:
    """
    Create a language choice field with consistent configuration.

    Args:
        required: Whether the field is required
        empty_label: Label for empty option (None = no empty option)
        use_pk: If True, returns ModelChoiceField with Language objects;
                if False, returns ChoiceField with code strings

    Returns:
        ModelChoiceField (if use_pk=True) or ChoiceField (if use_pk=False)

    Example:
        # For extraction forms (uses ModelChoiceField, returns Language objects)
        self.fields["language"] = create_language_field()

        # For subscription forms (uses ModelChoiceField, allows "All")
        self.fields["language"] = create_language_field(empty_label="All")

        # For search forms (uses ChoiceField with codes)
        self.fields["language"] = create_language_field(use_pk=False)
    """
    languages = Language.objects.order_by("code")

    if use_pk:
        # Return ModelChoiceField - cleaned_data will contain Language objects
        field = forms.ModelChoiceField(
            queryset=languages,
            required=required,
            empty_label=empty_label,
        )
        field.label_from_instance = lambda obj: LANGUAGE_LABELS[obj.code]
        return field
    else:
        # Return ChoiceField - cleaned_data will contain code strings
        choices = [(lang.code, LANGUAGE_LABELS[lang.code]) for lang in languages]
        if empty_label is not None:
            choices.insert(0, ("", empty_label))
        field = forms.ChoiceField(required=required, choices=choices)
        return field


@overload
def create_modality_field(
    required: bool = False,
    widget_size: int = 6,
    use_pk: Literal[True] = True,
) -> forms.ModelMultipleChoiceField: ...


@overload
def create_modality_field(
    required: bool = False,
    widget_size: int = 6,
    use_pk: Literal[False] = False,
) -> forms.MultipleChoiceField: ...


def create_modality_field(
    required: bool = False,
    widget_size: int = 6,
    use_pk: bool = True,
) -> forms.ModelMultipleChoiceField | forms.MultipleChoiceField:
    """
    Create a modality multiple choice field with consistent configuration.

    Args:
        required: Whether the field is required
        widget_size: Height of the select widget
        use_pk: If True, returns ModelMultipleChoiceField with Modality objects;
                if False, returns MultipleChoiceField with code strings

    Returns:
        ModelMultipleChoiceField (if use_pk=True) or MultipleChoiceField (if use_pk=False)

    Example:
        # For extraction forms (uses ModelMultipleChoiceField, returns Modality objects)
        self.fields["modalities"] = create_modality_field()

        # For search forms (uses MultipleChoiceField with codes)
        self.fields["modalities"] = create_modality_field(use_pk=False)
    """
    modalities = Modality.objects.filter(filterable=True).order_by("code")

    if use_pk:
        # Return ModelMultipleChoiceField - cleaned_data will contain Modality objects
        field = forms.ModelMultipleChoiceField(
            queryset=modalities,
            required=required,
        )
        # Display just the code for each modality
        field.label_from_instance = lambda obj: obj.code
        field.widget.attrs["size"] = widget_size
        return field
    else:
        # Return MultipleChoiceField - cleaned_data will contain code strings
        field = forms.MultipleChoiceField(required=required)
        field.choices = [(mod.code, mod.code) for mod in modalities]
        field.widget.attrs["size"] = widget_size
        return field


def create_age_range_fields() -> tuple[forms.IntegerField, forms.IntegerField]:
    """
    Create age_from and age_till fields with consistent configuration.

    Returns:
        Tuple of (age_from_field, age_till_field)

    Example:
        age_from, age_till = create_age_range_fields()
        self.fields["age_from"] = age_from
        self.fields["age_till"] = age_till
    """
    age_from = forms.IntegerField(
        required=False,
        min_value=MIN_AGE,
        max_value=MAX_AGE,
        widget=forms.NumberInput(
            attrs={
                "type": "range",
                "step": AGE_STEP,
                "value": MIN_AGE,
            }
        ),
    )

    age_till = forms.IntegerField(
        required=False,
        min_value=MIN_AGE,
        max_value=MAX_AGE,
        widget=forms.NumberInput(
            attrs={
                "type": "range",
                "step": AGE_STEP,
                "value": MAX_AGE,
            }
        ),
    )

    return age_from, age_till
