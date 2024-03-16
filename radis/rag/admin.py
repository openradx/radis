from django import forms
from django.contrib import admin

from .models import Question, QuestionResult, RagJob, RagTask


class QuestionInline(admin.StackedInline):
    model = Question
    extra = 1
    ordering = ("id",)


class RagJobAdmin(admin.ModelAdmin):
    inlines = [QuestionInline]


admin.site.register(RagJob, RagJobAdmin)


class QuestionResultInlineFormset(forms.BaseInlineFormSet):
    def add_fields(self, form: forms.Form, index: int) -> None:
        super().add_fields(form, index)
        task: RagTask = self.instance
        form.fields["question"].queryset = task.job.questions.all()


class QuestionResultInline(admin.StackedInline):
    model = QuestionResult
    extra = 1
    ordering = ("id",)
    formset = QuestionResultInlineFormset


class RagTaskAdmin(admin.ModelAdmin):
    inlines = [QuestionResultInline]


admin.site.register(RagTask, RagTaskAdmin)
