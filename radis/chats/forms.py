from crispy_forms.bootstrap import FieldWithButtons, StrictButton
from crispy_forms.helper import FormHelper, Layout
from crispy_forms.layout import Field
from django import forms


def create_prompt_layout(placeholder: str) -> Layout:
    return Layout(
        FieldWithButtons(
            Field(
                "prompt",
                placeholder=placeholder,
            ),
            StrictButton(
                """
                    {% load bootstrap_icon from common_extras %}
                    {% bootstrap_icon 'send-fill' %}
                    """,
                type="submit",
                name="send",
                value="true",
                css_class="btn-outline-primary",
            ),
        ),
    )


class CreateChatForm(forms.Form):
    report_id = forms.IntegerField(required=False)
    prompt = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 1, "x-data": True, "x-grow": True, "x-prompt": True}),
        max_length=1000,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.helper = FormHelper()
        self.helper.form_show_labels = False
        self.helper.form_tag = False

        layout = Layout()
        if self.initial.get("report_id"):
            prompt_placeholder = "Ask a question about this report..."
        else:
            prompt_placeholder = "Type a message..."

        layout.append(Field("report_id", type="hidden"))
        layout.append(create_prompt_layout(prompt_placeholder))

        self.helper = FormHelper()
        self.helper.form_show_labels = False
        self.helper.form_tag = False
        self.helper.layout = layout


class PromptForm(forms.Form):
    prompt = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 1, "x-data": True, "x-grow": True, "x-prompt": True}),
        max_length=1000,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.helper = FormHelper()
        self.helper.form_show_labels = False
        self.helper.form_tag = False
        self.helper.layout = create_prompt_layout("Type a message...")
