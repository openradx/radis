from radis.pgsearch.models import ReportSearchVector
from radis.search.site import ReportDocument


class AnnotatedReportSearchVector(ReportSearchVector):
    rank: float
    summary: str

    class Meta:
        abstract = True


def document_from_pgsearch_response(
    record: AnnotatedReportSearchVector,
) -> ReportDocument:
    report = record.report
    return ReportDocument(
        relevance=record.rank,
        document_id=report.document_id,
        pacs_name=report.pacs_name,
        pacs_link=report.pacs_link,
        patient_birth_date=report.patient_birth_date,
        patient_age=report.patient_age,
        patient_sex=report.patient_sex,  # type: ignore
        study_description=report.study_description,
        study_datetime=report.study_datetime,
        modalities=report.modality_codes,
        summary=record.summary,
    )
