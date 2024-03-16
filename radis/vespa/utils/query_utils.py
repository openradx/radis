from datetime import datetime, time

from radis.search.site import SearchFilters


def build_yql_filter(filters: SearchFilters) -> str:
    print(filters)
    q = ""
    if filters.study_date_from:
        df = datetime.combine(filters.study_date_from, time()).timestamp()
        q += f" and study_datetime > {df}"
    if filters.study_date_till:
        dt = datetime.combine(filters.study_date_till, time()).timestamp()
        q += f" and study_datetime < {dt}"
    if filters.study_description:
        q += f" and study_description contains '{filters.study_description}'"
    if filters.modalities:
        modalities = [f"'{m}'" for m in filters.modalities]
        q += f" and modalities in ({','.join(modalities)})"
    if filters.patient_sex:
        q += f" and patient_sex contains '{filters.patient_sex}'"
    if filters.patient_age_from:
        q += f" and patient_age > {filters.patient_age_from}"
    if filters.patient_age_till and filters.patient_age_till < 120:
        q += f" and patient_age < {filters.patient_age_till}"
    return q
