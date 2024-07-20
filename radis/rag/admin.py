from django import forms
from django.contrib import admin

from .models import Question, QuestionResult, RagInstance, RagJob, RagTask


class QuestionInline(admin.StackedInline):
    model = Question
    extra = 1
    ordering = ("id",)


class RagJobAdmin(admin.ModelAdmin):
    inlines = [QuestionInline]


admin.site.register(RagJob, RagJobAdmin)


class RagInstanceInline(admin.StackedInline):
    model = RagInstance
    extra = 1
    ordering = ("id",)


class RagTaskAdmin(admin.ModelAdmin):
    inlines = [RagInstanceInline]


admin.site.register(RagTask, RagTaskAdmin)


class QuestionResultInlineFormset(forms.BaseInlineFormSet):
    def add_fields(self, form: forms.Form, index: int) -> None:
        super().add_fields(form, index)
        rag_instance = self.instance
        form.fields["question"].queryset = rag_instance.task.job.questions.all()


class QuestionResultInline(admin.StackedInline):
    model = QuestionResult
    extra = 1
    ordering = ("id",)
    formset = QuestionResultInlineFormset


class RagInstanceAdmin(admin.ModelAdmin):
    inlines = [QuestionResultInline]


admin.site.register(RagInstance, RagInstanceAdmin)
