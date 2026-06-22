from crispy_forms.helper import FormHelper
from crispy_forms.layout import Column, Layout, Row
from django import forms

from .models import Question, QuestionSet


class QuestionSetForm(forms.ModelForm):
    class Meta:
        model = QuestionSet
        fields = ["name", "description", "is_active", "order"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(
            Row(
                Column("name", "description"),
                Column("is_active", "order", css_class="col-3"),
            )
        )


class QuestionForm(forms.ModelForm):
    class Meta:
        model = Question
        fields = ["label", "question", "is_active", "order"]

    def __init__(self, *args, **kwargs):
        self.question_set = kwargs.pop("question_set", None)
        super().__init__(*args, **kwargs)

        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(
            "label",
            "question",
            Row(Column("is_active"), Column("order", css_class="col-3")),
        )

        self.fields["question"].required = False
        self.fields["question"].help_text = "Optional. If left empty, the label is used."

    def clean_label(self):
        label = self.cleaned_data.get("label", "")
        if not label or not self.question_set:
            return label

        existing = Question.objects.filter(
            question_set=self.question_set, label__iexact=label
        )
        if self.instance and self.instance.pk:
            existing = existing.exclude(pk=self.instance.pk)
        if existing.exists():
            raise forms.ValidationError(
                "A question with this label already exists in this set."
            )
        return label
