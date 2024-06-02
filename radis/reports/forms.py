from crispy_forms.helper import FormHelper, Layout
from crispy_forms.layout import Field, Submit
from django import forms


class PromptForm(forms.Form):
    prompt = forms.CharField(max_length=500)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.helper = FormHelper()
        self.helper.form_show_labels = False
        self.helper.form_tag = False
        self.helper.layout = Layout(
            Field("prompt", placeholder="Ask the LLM a question about this report"),
            Submit(
                "yes_no_answer",
                "Yes/No answer",
                css_class="btn-primary",
            ),
            Submit(
                "full_answer",
                "Full answer",
                css_class="btn-primary",
            ),
        )
