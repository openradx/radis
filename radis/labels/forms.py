from crispy_forms.helper import FormHelper
from crispy_forms.layout import Column, Div, Field, Layout, Row
from django import forms

from radis.core.layouts import Formset

from .models import LabelChoice, LabelGroup, LabelQuestion


class LabelGroupForm(forms.ModelForm):
    class Meta:
        model = LabelGroup
        fields = [
            "name",
            "slug",
            "description",
            "is_active",
            "order",
        ]
        help_texts = {
            "slug": "URL-friendly identifier. Use lowercase and hyphens.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(
            Row(
                Column("name", "slug", "description"),
                Column("is_active", "order", css_class="col-3"),
            )
        )


class LabelQuestionForm(forms.ModelForm):
    class Meta:
        model = LabelQuestion
        fields = [
            "name",
            "question",
            "description",
            "is_active",
            "order",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(
            "name",
            "question",
            "description",
            Row(Column("is_active"), Column("order", css_class="col-3")),
            Formset("formset", legend="Choices", add_form_label="Add Choice"),
        )


class LabelChoiceForm(forms.ModelForm):
    class Meta:
        model = LabelChoice
        fields = [
            "value",
            "label",
            "is_unknown",
            "order",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["value"].required = False
        self.fields["label"].required = False

        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.disable_csrf = True
        self.helper.layout = Layout(
            Div(
                Field("id", type="hidden"),
                Field("DELETE", type="hidden"),
                Row(
                    Column("value"),
                    Column("label"),
                    Column("is_unknown", css_class="col-2"),
                    Column("order", css_class="col-2"),
                ),
            )
        )


LabelChoiceFormSet = forms.inlineformset_factory(
    LabelQuestion,
    LabelChoice,
    form=LabelChoiceForm,
    extra=1,
    min_num=1,
    validate_min=True,
    can_delete=True,
)
