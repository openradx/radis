from django.conf import settings
from procrastinate.contrib.django import app


def test_periodic_sweep_task_calls_sweep(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr("radis.core.tasks.sweep_stale_analysis_state", lambda: calls.append(1))

    from radis.core.tasks import sweep_stale_tasks_periodic

    sweep_stale_tasks_periodic(timestamp=0)
    assert calls == [1]


def test_periodic_sweep_task_registered_with_cron():
    registered = [
        pt
        for pt in app.periodic_registry.periodic_tasks.values()
        if pt.task.name == "radis.core.tasks.sweep_stale_tasks_periodic"
    ]
    assert len(registered) == 1
    assert registered[0].cron == settings.ANALYSIS_SWEEP_CRON
    assert registered[0].task.queueing_lock == "sweep_stale_tasks"
