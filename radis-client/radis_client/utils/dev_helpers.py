import uuid
from datetime import timezone
from typing import Any, Literal, cast

from faker import Faker
from pydicom.uid import generate_uid
from requests import HTTPError

from radis_client.client import RadisClient, ReportData

faker = Faker()


def create_report_data(
    body: str,
    params: dict[str, Any],
) -> ReportData:
    metadata = {
        "series_instance_uid": str(generate_uid()),
        "sop_instance_uid": str(generate_uid()),
    }

    return ReportData(
        document_id=str(uuid.uuid4()),
        language=params.get("lng") or "en",
        groups=[params.get("group_id") or 1],
        pacs_aet=faker.bothify("AE####").upper(),
        pacs_name=faker.company(),
        pacs_link=faker.url(),
        patient_id=params.get("patient_id") or faker.numerify("##########"),
        patient_birth_date=params.get("patient_birthdate")
        or faker.date_of_birth(minimum_age=25, maximum_age=90),
        patient_sex=cast(
            Literal["M", "F", "O"],
            params.get("patient_sex") or faker.random_element(elements=("M", "F", "O")),
        ),
        study_description=params.get("study_description") or faker.text(max_nb_chars=64),
        study_datetime=params.get("study_date")
        or faker.date_time_between(start_date="-5y", end_date="now", tzinfo=timezone.utc),
        study_instance_uid=generate_uid(),
        accession_number=faker.numerify("############"),
        modalities=[params.get("modality") or "CT"],
        metadata=metadata,
        body=body,
    )


def upload_reports(
    bodies: list[str], params: dict[str, Any], api_url: str, auth_token: str
) -> tuple[str, int, int]:
    client = RadisClient(api_url, auth_token)

    reports_url = f"{api_url}/api/reports/"
    print(f"Uploading example report(s) to {reports_url}...")
    succeeded = 0
    failed = 0
    for body in bodies:
        report_data = create_report_data(body, params)
        try:
            client.create_report(report_data)
            succeeded += 1
            print(".", end="", flush=True)
        except HTTPError as e:
            failed += 1
            print("x", end="", flush=True)
            print(f"Error creating report: {e}")
    print("")
    return reports_url, succeeded, failed
