def create_mappings(analyzer: str) -> dict:
    return {
        "properties": {
            "document_id": {"type": "keyword"},
            "language": {"type": "keyword"},
            "groups": {"type": "keyword"},
            "pacs_aet": {"type": "keyword"},
            "pacs_name": {"type": "keyword"},
            "pacs_link": {"type": "keyword"},
            "patient_birth_date": {"type": "date"},
            "patient_age": {"type": "integer"},
            "patient_sex": {"type": "keyword"},
            "study_datetime": {"type": "date"},
            "modalities": {"type": "keyword"},
            "body": {"type": "text", "analyzer": analyzer},
        }
    }
