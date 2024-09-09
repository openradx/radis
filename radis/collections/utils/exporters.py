from io import BytesIO

import pandas as pd
from django.utils import formats

from ..models import Collection


def export_collection(collection: Collection) -> BytesIO:
    header = (
        "PACS",
        "Patient ID",
        "Patient Birth Date",
        "Study Date",
        "Study Description",
        "Study Instance UID",
        "Accession Number",
        "Modalities",
        "Content",
    )

    rows = []
    for report in collection.reports.all():
        birth_date = formats.date_format(report.patient_birth_date, "SHORT_DATE_FORMAT")
        study_date = formats.date_format(report.study_datetime, "SHORT_DATE_FORMAT")
        modalities = ", ".join(modality.code for modality in report.modalities.all())

        row = (
            report.pacs_name,
            report.patient_id,
            birth_date,
            study_date,
            report.study_description,
            report.study_instance_uid,
            report.accession_number,
            modalities,
            report.body,
        )
        rows.append(row)

    file = BytesIO()
    with pd.ExcelWriter(
        file,  # type: ignore
        date_format=formats.get_format("SHORT_DATE_FORMAT"),
        engine="openpyxl",
    ) as writer:
        df = pd.DataFrame(rows, columns=header)  # type: ignore
        df.to_excel(writer, index=False)

    return file
