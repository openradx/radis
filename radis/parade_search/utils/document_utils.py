from radis.pgsearch.models import ReportSearchVector
from radis.search.site import ReportDocument


class AnnotatedReportSearchVector(ReportSearchVector):
    rank: float
    summary: str

    class Meta:
        abstract = True


def find_all_occurrences(text, substring):
    """
    Returns a list of all occurrences of the substring in the text.

    :param text: The string to search within.
    :param substring: The substring to search for.
    :return: A list of indices where the substring occurs in the text.
    """
    indices = []
    start = 0
    while True:
        index = text.find(substring, start)
        if index == -1:
            break
        indices.append(index)
        start = index + 1  # Move start to the next character after the found substring
    return indices


def snippet(text: str, start_tag: str, max_num_chars: int) -> list:
    indices = find_all_occurrences(text, start_tag)
    snippets = []
    last_end = 0
    for index in indices:
        if index < last_end:
            continue
        start = max(0, text.rfind(" ", 0, index - max_num_chars))
        end = text.find(" ", index + len(start_tag) + max_num_chars + 8)
        if end == -1:
            end = len(text)
        snippets.append(text[start:end])
        last_end = end

    return snippets


def summarize(snippets):
    if len(snippets) == 1:
        return f"...{snippets[0]}..."
    string = " ... ".join(snippets)
    string = "..." + string + "..."
    return string


def document_from_pgsearch_response(
    record: AnnotatedReportSearchVector,
) -> ReportDocument:
    report = record
    print(report.summary)
    return ReportDocument(
        relevance=record.rank,
        document_id=report.document_id,
        pacs_name=report.pacs_name,
        pacs_link=report.pacs_link,
        patient_age=report.patient_age,
        patient_sex=report.patient_sex,
        study_description=report.study_description,
        modalities=report.modality_codes,
        summary=summarize(snippet(report.summary, "<b><u>", 30)),
    )
