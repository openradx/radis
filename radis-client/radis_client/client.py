from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any, Literal

import requests


@dataclass
class ReportData:
    document_id: str
    language: str
    groups: list[int]
    pacs_aet: str
    pacs_name: str
    pacs_link: str
    patient_id: str
    patient_birth_date: date
    patient_sex: Literal["M", "F", "O"]
    study_description: str
    study_datetime: datetime
    study_instance_uid: str
    accession_number: str
    modalities: list[str]
    metadata: dict[str, str]
    body: str

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)

        if isinstance(data["patient_birth_date"], date):
            data["patient_birth_date"] = data["patient_birth_date"].isoformat()
        if isinstance(data["study_datetime"], datetime):
            data["study_datetime"] = data["study_datetime"].isoformat()

        return data


class RadisClient:
    def __init__(self, server_url: str, auth_token: str):
        self.server_url = server_url
        self.auth_token = auth_token

        self._reports_url = f"{self.server_url}/api/reports/"
        self._headers = {"Authorization": f"Token {self.auth_token}"}

    def create_report(self, report_data: ReportData) -> dict[str, Any]:
        """Create a report using the provided data and return the response as a dictionary.

        Args:
            data: The data to be used for creating the report.

        Returns:
            The response from the report creation request.
        """
        response = requests.post(
            self._reports_url, json=report_data.to_dict(), headers=self._headers
        )
        response.raise_for_status()
        return response.json()

    def retrieve_report(self, document_id: str, full: bool = False) -> dict[str, Any]:
        """Retrieve a report with the given document ID.

        Args:
            document_id: The ID of the document to retrieve.
            full: Whether to retrieve also document data from Vespa.
                Defaults to False.

        Returns:
            The retrieved report in dictionary format.
        """
        response = requests.get(
            f"{self._reports_url}{document_id}/",
            headers=self._headers,
            params={"full": full},
        )
        response.raise_for_status()
        return response.json()

    def update_report(
        self, document_id: str, report_data: ReportData, upsert=False
    ) -> dict[str, Any]:
        """Update a report with the given document ID and report data.

        Partial updates are not supported.

        Args:
            document_id: The ID of the document to be updated.
            data: The report data to be updated.
            upsert: Whether to perform an upsert if the document is not found.

        Returns:
            The response as JSON.
        """
        response = requests.put(
            f"{self._reports_url}{document_id}/",
            json=report_data.to_dict(),
            headers=self._headers,
            params={"upsert": upsert},
        )
        response.raise_for_status()
        return response.json()

    def delete_report(self, document_id: str) -> None:
        """
        Deletes a report with the given document_id.

        Args:
            document_id: The ID of the document to be deleted.
        """
        response = requests.delete(f"{self._reports_url}{document_id}/", headers=self._headers)
        response.raise_for_status()
