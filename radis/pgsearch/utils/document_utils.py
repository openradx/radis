import re

from django.utils.html import escape
from django.utils.safestring import SafeString, mark_safe

from radis.pgsearch.models import ReportSearchIndex
from radis.search.site import ReportDocument

# Markers ts_headline puts around highlighted words. Control characters that no
# ingestion path produces; summary_markup tolerates strays anyway.
HEADLINE_START = "\x02"
HEADLINE_STOP = "\x03"
_HIGHLIGHT = re.compile(f"{HEADLINE_START}([^{HEADLINE_START}{HEADLINE_STOP}]*){HEADLINE_STOP}")


class AnnotatedReportSearchIndex(ReportSearchIndex):
    """Typing shim for the annotations the hydration queryset adds to each row."""

    rank: float | None
    headline: str

    class Meta:
        abstract = True


def summary_markup(body: str, headline: str, max_words: int = 30) -> SafeString:
    """The excerpt shown on a result card, as HTML-safe markup.

    ``headline`` is the ts_headline output carrying the sentinel markers. When it is empty
    (no tsquery ran, as in a semantic search, or the row's language is not among those the
    query was built for) the first ``max_words`` words of the body are shown instead;
    ts_headline itself returns a body's opening words when nothing in it matches. The text
    is escaped first, balanced marker pairs then become <em>, and stray markers are
    dropped, so report text can never contribute markup.
    """
    text = headline if headline else " ".join(body.split()[:max_words])
    escaped = str(escape(text))
    highlighted = _HIGHLIGHT.sub(r"<em>\1</em>", escaped)
    cleaned = highlighted.replace(HEADLINE_START, "").replace(HEADLINE_STOP, "")
    return mark_safe(cleaned)


def document_from_pgsearch_response(
    record: ReportSearchIndex,
    *,
    relevance: float | None,
    summary: str,
    similarity: float | None,
    score: float,
) -> ReportDocument:
    report = record.report
    return ReportDocument(
        relevance=relevance,
        document_id=report.document_id,
        pacs_name=report.pacs_name,
        pacs_link=report.pacs_link,
        patient_age=report.patient_age,
        patient_sex=report.patient_sex,
        study_description=report.study_description,
        modalities=report.modality_codes,
        summary=summary,
        similarity=similarity,
        score=score,
    )
