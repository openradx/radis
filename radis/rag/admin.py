from django import forms
from django.contrib import admin

from .models import Question, QuestionResult, RagJob, RagReportInstance, RagTask


class QuestionInline(admin.StackedInline):
    model = Question
    extra = 1
    ordering = ("id",)


class RagJobAdmin(admin.ModelAdmin):
    inlines = [QuestionInline]


admin.site.register(RagJob, RagJobAdmin)


class RagReportInstanceInline(admin.StackedInline):
    model = RagReportInstance
    extra = 1
    ordering = ("id",)


class RagTaskAdmin(admin.ModelAdmin):
    inlines = [RagReportInstanceInline]


admin.site.register(RagTask, RagTaskAdmin)


class QuestionResultInlineFormset(forms.BaseInlineFormSet):
    def add_fields(self, form: forms.Form, index: int) -> None:
        super().add_fields(form, index)
        report_instance = self.instance
        form.fields["question"].queryset = report_instance.task.job.questions.all()


class QuestionResultInline(admin.StackedInline):
    model = QuestionResult
    extra = 1
    ordering = ("id",)
    formset = QuestionResultInlineFormset


class RagReportInstanceAdmin(admin.ModelAdmin):
    inlines = [QuestionResultInline]


admin.site.register(RagReportInstance, RagReportInstanceAdmin)
