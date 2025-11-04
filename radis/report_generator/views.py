from __future__ import annotations

import logging
from typing import Iterable

import openai
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.views import View

from radis.chats.utils.chat_client import _get_base_url
from radis.reports.factories import ReportFactory
from radis.reports.models import Language, Report

from .forms import GenerateReportForm
from .prompts import REPORT_SYSTEM_PROMPT

logger = logging.getLogger(__name__)


class ReportBodyGenerator:
    def __init__(self) -> None:
        base_url = _get_base_url()
        api_key = settings.EXTERNAL_LLM_PROVIDER_API_KEY
        self._client = openai.OpenAI(base_url=base_url, api_key=api_key)
        self._model_name = settings.LLM_MODEL_NAME

    def generate(self, user_prompt: str) -> str:
        try:
            completion = self._client.chat.completions.create(
                model=self._model_name,
                messages=[
                    {"role": "system", "content": REPORT_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
            )
        except Exception:  # noqa: BLE001
            logger.exception("LLM report generation failed")
            raise

        message = completion.choices[0].message.content
        if not message:
            raise ValueError("LLM returned an empty response")
        return message.strip()


def _build_user_prompt(instruction: str, context_lines: Iterable[str]) -> str:
    context_block = "\n".join(context_lines)
    base_instruction = (
        instruction.strip()
        if instruction.strip()
        else "Generate a realistic radiology report for development testing."
    )
    if context_block:
        return f"{base_instruction}\n\nContext:\n{context_block}"
    return base_instruction


class GenerateReportView(LoginRequiredMixin, UserPassesTestMixin, View):
    template_name = "report_generator/generate.html"
    form_class = GenerateReportForm

    def test_func(self) -> bool | None:
        return bool(settings.DEBUG)

    def handle_no_permission(self) -> HttpResponse:
        raise PermissionDenied

    def get(self, request: HttpRequest) -> HttpResponse:
        form = self.form_class()
        return render(request, self.template_name, {"form": form, "created_reports": []})

    def post(self, request: HttpRequest) -> HttpResponse:
        form = self.form_class(request.POST)
        created_reports: list[Report] = []
        if form.is_valid():
            active_group = Group.objects.order_by("id").first()
            if active_group is None:
                form.add_error(None, "You need an active group to create reports.")
            else:
                language_code = form.cleaned_data.get("language")
                language_obj: Language | None = None
                if language_code:
                    language_obj, _ = Language.objects.get_or_create(code=language_code)

                instruction = form.cleaned_data.get("instruction", "")

                context_lines = []
                if language_obj:
                    context_lines.append(f"Language: {language_obj.code}")
                for field, label in (
                    ("patient_id", "Patient ID"),
                    ("patient_birth_date", "Patient birth date"),
                    ("patient_sex", "Patient sex"),
                    ("study_description", "Study description"),
                    ("study_datetime", "Study date and time"),
                    ("study_instance_uid", "Study instance UID"),
                    ("accession_number", "Accession number"),
                ):
                    value = form.cleaned_data.get(field)
                    if value:
                        context_lines.append(f"{label}: {value}")

                modalities = form.cleaned_data.get("modalities")
                if modalities:
                    context_lines.append(f"Modalities: {', '.join(modalities)}")

                prompt = _build_user_prompt(instruction, context_lines)
                generator = ReportBodyGenerator()
                count = form.cleaned_data.get("count") or 1

                try:
                    for _ in range(count):
                        body = generator.generate(prompt)
                        report_kwargs = form.get_report_kwargs()
                        report_kwargs["body"] = body
                        if language_obj is not None:
                            report_kwargs["language"] = language_obj
                        if modalities:
                            report_kwargs["modalities"] = modalities
                        report = ReportFactory.create(**report_kwargs)
                        report.groups.set([active_group])
                        created_reports.append(report)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Failed to generate report")
                    form.add_error(
                        None,
                        f"Could not generate report: {exc}",
                    )
                    created_reports.clear()

                if created_reports:
                    messages.success(
                        request,
                        f"Created {len(created_reports)} report(s) using the LLM.",
                    )

        context = {"form": form, "created_reports": created_reports}
        return render(request, self.template_name, context)
