from django.db.models import F, Func

from radis.reports.models import Report


def fetch_available_modalities() -> list[str]:
    # TODO: Put an index somehow to modalities_in_study.
    # Not sure if there is really a way to do that.
    # Maybe this helps: https://stackoverflow.com/a/4059785/166229
    modalities = (
        Report.objects.annotate(modalities=Func(F("modalities_in_study"), function="unnest"))
        .values_list("modalities", flat=True)
        .distinct()
    )
    return sorted(modalities)
