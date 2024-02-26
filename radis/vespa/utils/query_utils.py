from datetime import datetime, time

from radis.search.site import Search


def build_yql(search: Search) -> str:
    q = "select * from sources * where userQuery()"

    f = search.filters
    if f.study_date_from:
        df = datetime.combine(f.study_date_from, time()).timestamp()
        q += f" and study_datetime > {df}"
    if f.study_date_till:
        dt = datetime.combine(f.study_date_till, time()).timestamp()
        q += f" and study_datetime < {dt}"
    if f.study_description:
        q += f" and study_description contains '{f.study_description}'"
    if f.modalities:
        modalities = [f"'{m}'" for m in f.modalities]
        q += f" and modalities_in_study in ({','.join(modalities)})"
    if f.patient_sex:
        q += f" and patient_sex = '{f.patient_sex}'"
    if f.patient_age_from:
        q += f" and patient_age > {f.patient_age_from}"
    if f.patient_age_till:
        q += f" and patient_age < {f.patient_age_till}"

    return q
