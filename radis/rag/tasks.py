import logging
from string import Template
from typing import Iterator, override

from django.conf import settings
from openai import OpenAI

from radis.celery import app as celery_app
from radis.core.tasks import ProcessAnalysisJob, ProcessAnalysisTask
from radis.reports.models import Report
from radis.search.site import Search, SearchFilters

from .models import Answer, QuestionResult, RagJob, RagTask
from .site import retrieval_providers

logger = logging.getLogger(__name__)

GRAMMAR = """
    root ::= Answer
    Answer ::= "$yes" | "$no"
"""


class ProcessRagTask(ProcessAnalysisTask):
    analysis_task_class = RagTask

    def __init__(self) -> None:
        super().__init__()

        self._client = OpenAI(base_url=f"{settings.LLAMACPP_URL}/v1", api_key="none")

    @override
    def process_task(self, task: RagTask) -> None:
        report_body = task.report.body
        language = task.report.language

        if language not in settings.RAG_SUPPORTED_LANGUAGES:
            raise ValueError(f"Language {language} is not supported by RAG.")

        all_results: list[RagTask.Result] = []

        system_prompt = settings.RAG_SYSTEM_PROMPT[language]
        logger.debug("Using system prompt:\n%s", system_prompt)

        grammar = Template(GRAMMAR).substitute(
            {
                "yes": settings.RAG_ANSWER_YES[language],
                "no": settings.RAG_ANSWER_NO[language],
            }
        )
        logger.debug("Using grammar:\n%s", grammar)

        for question in task.job.questions.all():
            user_prompt = Template(settings.RAG_USER_PROMPT[language]).substitute(
                {"report": report_body, "question": question.question}
            )

            logger.debug("Sending user prompt:\n%s", user_prompt)

            completion = self._client.chat.completions.create(
                model="none",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                extra_body={"grammar": grammar},
            )

            llm_answer = completion.choices[0].message.content
            logger.debug("Received answer by LLM: %s", llm_answer)

            if llm_answer == settings.RAG_ANSWER_YES[language]:
                answer = Answer.YES
            elif llm_answer == settings.RAG_ANSWER_NO[language]:
                answer = Answer.NO
            else:
                raise ValueError(f"Unexpected answer: {llm_answer}")

            result = (
                RagTask.Result.ACCEPTED
                if question.accepted_answer == answer
                else RagTask.Result.REJECTED
            )

            QuestionResult.objects.update_or_create(
                task=task,
                question=question,
                defaults={
                    "original_answer": answer,
                    "current_answer": answer,
                    "result": result,
                },
            )

            all_results.append(result)

            if all([result == RagTask.Result.ACCEPTED for result in all_results]):
                task.overall_result = RagTask.Result.ACCEPTED
            else:
                task.overall_result = RagTask.Result.REJECTED

            logger.info(
                "RAG task %s finished with overall result: %s",
                task,
                task.get_overall_result_display(),
            )

            task.save()


process_rag_task = ProcessRagTask()


celery_app.register_task(process_rag_task)


class ProcessRagJob(ProcessAnalysisJob):
    analysis_job_class = RagJob
    process_analysis_task = process_rag_task
    task_queue = "llm_queue"

    @override
    def collect_tasks(self, job: RagJob) -> Iterator[RagTask]:
        patient_sex = None
        if job.patient_sex == "M":
            patient_sex = "M"
        elif job.patient_sex == "F":
            patient_sex = "F"

        provider = job.provider
        retrieval_provider = retrieval_providers[provider]

        search = Search(
            query=job.query,
            offset=0,
            limit=retrieval_provider.max_results,
            filters=SearchFilters(
                study_date_from=job.study_date_from,
                study_date_till=job.study_date_till,
                study_description=job.study_description,
                modalities=job.modalities,
                patient_sex=patient_sex,
                patient_age_from=job.age_from,
                patient_age_till=job.age_till,
            ),
        )

        logger.debug("Searching reports for task with search: %s", search)

        result = retrieval_provider.handler(search)

        for document_id in result.document_ids:
            task = RagTask.objects.create(
                job=job, report=Report.objects.get(document_id=document_id)
            )
            yield task


process_rag_job = ProcessRagJob()

celery_app.register_task(process_rag_job)
