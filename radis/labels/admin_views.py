from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.db import IntegrityError, transaction
from django.http import HttpRequest, HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.views.decorators.http import require_POST

from radis.core.models import AnalysisJob

from .models import LabelingJob


@staff_member_required
@require_POST
def run_backfill_view(request: HttpRequest) -> HttpResponseRedirect:
    target = reverse("admin:labels_labelingjob_changelist")
    if LabelingJob.objects.filter(status__in=LabelingJob.ACTIVE_STATUSES).exists():
        messages.error(request, "Another backfill is already active.")
        return HttpResponseRedirect(target)
    try:
        with transaction.atomic():
            job = LabelingJob.objects.create(
                owner=request.user, status=AnalysisJob.Status.UNVERIFIED
            )
        job.delay()
        messages.success(request, f"Backfill job #{job.id} started.")
    except IntegrityError:
        messages.error(request, "Another backfill just started; please refresh.")
    return HttpResponseRedirect(target)


@staff_member_required
@require_POST
def cancel_backfill_view(request: HttpRequest, job_id: int) -> HttpResponseRedirect:
    target = reverse("admin:labels_labelingjob_changelist")
    job = get_object_or_404(LabelingJob, id=job_id)
    if job.status not in LabelingJob.ACTIVE_STATUSES:
        messages.error(request, "Job is not in a cancelable state.")
    else:
        job.status = AnalysisJob.Status.CANCELING
        job.save(update_fields=["status"])
        messages.success(request, f"Backfill job #{job.id} canceling.")
    return HttpResponseRedirect(target)
