import json
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from adit_radis_shared.accounts.factories import UserFactory
from django.conf import settings
from django.test import Client, override_settings

from radis.labels.factories import LabelFactory, LabelGroupFactory
from radis.labels.labeling import label_report
from radis.labels_lab.decision_client import Decision, DecisionModelError
from radis.labels_lab.tests.helpers import FakeLLM
from radis.reports.factories import ReportFactory

DEFAULT_THRESHOLDS = {
    "gate": "0.5",
    "possible": "0.35",
    "likely": "0.6",
    "present": "0.85",
}


class FakeDecisionClient:
    """Stands in for the HTTP client: answers with a canned response, keeps the calls."""

    def __init__(self, response=None, error=None, models=(), models_error=None):
        self.response = response
        self.error = error
        self.models = models
        self.models_error = models_error
        self.calls: list[dict] = []

    def decide(self, state, questions, model, extras=None):
        self.calls.append(
            {"state": state, "questions": questions, "model": model, "extras": extras}
        )
        if self.error:
            raise self.error
        assert self.response is not None, "this fake was given no response to answer with"
        request = {"model": model, "state": state, "questions": questions}
        return Decision(request=request, response=self.response, elapsed_ms=12.3)

    def list_models(self):
        if self.models_error:
            raise self.models_error
        return list(self.models)


@contextmanager
def _decision_model(**kwargs):
    fake = FakeDecisionClient(**kwargs)
    with patch("radis.labels_lab.views.DecisionClient", return_value=fake):
        yield fake


def _llm(gate="YES", pneumonia="PRESENT", pneumothorax="ABSENT"):
    fake = FakeLLM(
        gate_values={"Chest pathology": gate},
        label_values={"pneumonia": pneumonia, "pneumothorax": pneumothorax},
    )
    return patch("radis.labels_lab.baseline.LLMClient", return_value=fake)


class RecordingLLM:
    """Says YES to every gate and ABSENT to every label, and keeps what it was sent."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def extract_data(self, prompt, schema, max_wait=None):
        # The schema is kept as text: the order of its fields is part of what the LLM is
        # sent, and comparing dicts would not see it.
        self.calls.append((prompt, json.dumps(schema.model_json_schema())))
        value = "YES" if schema.__name__ == "GateScreening" else "ABSENT"
        return schema.model_validate(dict.fromkeys(schema.model_fields, value))


@contextmanager
def _llm_left_alone():
    """Fails the test if the block reaches for the LLM.

    Checked on the way out rather than by raising from the client, because the run view
    reports LLM errors on the page instead of letting them escape.
    """
    with patch("radis.labels_lab.baseline.LLMClient") as llm_client:
        yield
    assert llm_client.call_count == 0, "the LLM was asked"


@pytest.fixture
def staff_client(client: Client) -> Client:
    client.force_login(UserFactory.create(is_active=True, is_staff=True))
    return client


@pytest.fixture
def chest():
    group = LabelGroupFactory.create(
        name="Chest pathology", gate_question="Does this report describe imaging of the chest?"
    )
    pneumonia = LabelFactory.create(
        group=group, name="pneumonia", description="Infectious consolidation of the lung."
    )
    pneumothorax = LabelFactory.create(
        group=group, name="pneumothorax", description="Air in the pleural space."
    )
    return group, pneumonia, pneumothorax


def _decide_response(group, pneumonia, pneumothorax, **overrides) -> dict:
    """An /api/decide response for the `chest` fixture, as Ollaya documents it."""
    response = {
        "model": "laya:en",
        "answers": {
            f"gate_noul:{group.id}": {"type": "noul", "noul": 0.9127},
            f"gate_choice:{group.id}": {
                "type": "choice",
                "choice": "A",
                "confidence": 0.7,
                "probabilities": {"A": 0.85, "B": 0.15},
            },
            f"bucket:{pneumonia.id}": {
                "type": "choice",
                "choice": "LIKELY",
                "confidence": 0.5614,
                "probabilities": {
                    "PRESENT": 0.2,
                    "LIKELY": 0.6491,
                    "POSSIBLE": 0.1,
                    "ABSENT": 0.03,
                    "UNMENTIONED": 0.0209,
                },
            },
            f"present:{pneumonia.id}": {"type": "noul", "noul": 0.7},
            f"bucket:{pneumothorax.id}": {
                "type": "choice",
                "choice": "ABSENT",
                "confidence": 0.75,
                "probabilities": {
                    "PRESENT": 0.05,
                    "LIKELY": 0.05,
                    "POSSIBLE": 0.05,
                    "ABSENT": 0.8,
                    "UNMENTIONED": 0.05,
                },
            },
            f"present:{pneumothorax.id}": {"type": "noul", "noul": 0.05},
        },
        "usage": {"input_tokens": 711, "output_tokens": 0},
        "routing": {
            "router": "laya:latest",
            "model": "laya:en",
            "route": "english",
            "reason": "English Latin text",
        },
        "state_truncated": False,
        "done_reason": "decide",
        "created_at": "2026-09-24T09:30:12.418Z",
        "total_duration": 18734512,
        "load_duration": 0,
        "eval_duration": 16302117,
    }
    response.update(overrides)
    return response


def _run_form(group, **overrides) -> dict:
    data = {
        "text": "Lungs are clear.",
        "groups": [group.id],
        "model": "laya:latest",
        "run_baseline": "on",
        "extras": "on",
        **DEFAULT_THRESHOLDS,
        **overrides,
    }
    return {name: value for name, value in data.items() if value is not None}


@pytest.mark.django_db
@pytest.mark.parametrize(
    "method, url",
    [
        ("get", "/labels-lab/"),
        ("get", "/labels-lab/text/?source=en"),
        ("post", "/labels-lab/run/"),
        ("post", "/labels-lab/remap/"),
    ],
)
def test_lab_is_closed_to_users_who_are_not_staff(client: Client, method, url):
    client.force_login(UserFactory.create(is_active=True, is_staff=False))

    response = getattr(client, method)(url)

    assert response.status_code == 302
    assert "login" in response["Location"]


@pytest.mark.django_db
def test_lab_offers_the_groups_with_active_labels_and_the_models_of_the_endpoint(
    staff_client: Client, chest
):
    retired = LabelGroupFactory.create(name="Retired group")
    LabelFactory.create(group=retired, active=False)

    with _decision_model(models=["laya:en", "laya:latest", "laya:multilingual"]):
        response = staff_client.get("/labels-lab/")

    content = response.content.decode()
    assert response.status_code == 200
    assert "Chest pathology" in content
    assert "Retired group" not in content
    for model in ("laya:en", "laya:latest", "laya:multilingual"):
        assert f'value="{model}"' in content


@pytest.mark.django_db
@override_settings(DECISION_MODEL="laya:configured")
def test_lab_falls_back_to_the_configured_model_when_the_endpoint_is_down(
    staff_client: Client, chest
):
    with _decision_model(models_error=DecisionModelError("Could not reach http://ollaya.test")):
        response = staff_client.get("/labels-lab/")

    content = response.content.decode()
    assert response.status_code == 200
    assert 'value="laya:configured"' in content
    assert "Could not reach http://ollaya.test" in content


@pytest.mark.django_db
def test_text_endpoint_loads_a_report_by_primary_key(staff_client: Client):
    report = ReportFactory.create(body="Kein Nachweis einer Pneumonie.")

    response = staff_client.get("/labels-lab/text/", {"source": "report", "report": report.pk})

    assert response.context["text"] == "Kein Nachweis einer Pneumonie."
    assert "Kein Nachweis einer Pneumonie." in response.content.decode()


@pytest.mark.django_db
def test_text_endpoint_loads_a_report_by_document_id(staff_client: Client):
    ReportFactory.create(document_id="DOC-4711", body="Unauffälliger Befund.")

    response = staff_client.get("/labels-lab/text/", {"source": "report", "report": "DOC-4711"})

    assert response.context["text"] == "Unauffälliger Befund."


@pytest.mark.django_db
def test_text_endpoint_leaves_the_text_alone_when_the_report_does_not_exist(
    staff_client: Client,
):
    response = staff_client.get("/labels-lab/text/", {"source": "report", "report": "nope"})

    content = response.content.decode()
    assert response.status_code == 200
    assert (response["HX-Retarget"], response["HX-Reswap"]) == ("#lab-text-info", "innerHTML")
    assert "<textarea" not in content
    assert "nope" in content


@pytest.mark.django_db
def test_text_endpoint_takes_digits_that_are_no_number_for_a_document_id(staff_client: Client):
    response = staff_client.get("/labels-lab/text/", {"source": "report", "report": "²"})

    assert response.status_code == 200
    assert response["HX-Retarget"] == "#lab-text-info"


@pytest.mark.django_db
@pytest.mark.parametrize("language", ["en", "de"])
def test_text_endpoint_loads_a_sample_report_of_the_requested_language(
    staff_client: Client, language
):
    sample_file = settings.BASE_PATH / "samples" / f"reports_{language}.json"
    samples = json.loads(sample_file.read_text(encoding="utf-8"))

    response = staff_client.get("/labels-lab/text/", {"source": language})

    assert response.context["text"] in samples


@pytest.mark.django_db
def test_run_asks_the_decision_model_about_the_posted_text(staff_client: Client, chest):
    group, pneumonia, pneumothorax = chest

    with _decision_model(response=_decide_response(*chest)) as decision_model, _llm():
        staff_client.post("/labels-lab/run/", _run_form(group, model="laya:multilingual"))

    [call] = decision_model.calls
    assert call["state"] == "Lungs are clear."
    assert call["model"] == "laya:multilingual"
    assert call["extras"] == ["laya"]
    assert set(call["questions"]) == {
        f"gate_noul:{group.id}",
        f"gate_choice:{group.id}",
        f"bucket:{pneumonia.id}",
        f"present:{pneumonia.id}",
        f"bucket:{pneumothorax.id}",
        f"present:{pneumothorax.id}",
    }


@pytest.mark.django_db
def test_run_omits_the_native_extras_when_they_are_switched_off(staff_client: Client, chest):
    group, *_ = chest

    with _decision_model(response=_decide_response(*chest)) as decision_model, _llm():
        staff_client.post("/labels-lab/run/", _run_form(group, extras=None))

    assert decision_model.calls[0]["extras"] is None


@pytest.mark.django_db
def test_run_lines_up_the_decision_model_with_the_llm(staff_client: Client, chest):
    group, *_ = chest

    with _decision_model(response=_decide_response(*chest)), _llm():
        response = staff_client.post("/labels-lab/run/", _run_form(group))

    [view] = response.context["group_views"]
    assert (view.gate.llm, view.gate.noul_value, view.gate.choice_value) == ("YES", "YES", "YES")
    assert [(lbl.spec.name, lbl.llm, lbl.choice, lbl.noul_value) for lbl in view.labels] == [
        ("pneumonia", "PRESENT", "LIKELY", "LIKELY"),
        ("pneumothorax", "ABSENT", "ABSENT", "ABSENT / UNMENTIONED"),
    ]
    assert "ABSENT / UNMENTIONED" in response.content.decode()


@pytest.mark.django_db
def test_run_shows_the_native_response_of_the_decision_model(staff_client: Client, chest):
    group, *_ = chest

    with _decision_model(response=_decide_response(*chest)), _llm():
        response = staff_client.post("/labels-lab/run/", _run_form(group))

    content = response.content.decode()
    assert "English Latin text" in content  # routing reason, only in the native response
    assert "eval_duration" in content
    assert "did not fit" not in content


@pytest.mark.django_db
def test_run_warns_when_the_report_did_not_fit_the_models_context(staff_client: Client, chest):
    group, *_ = chest
    truncated = _decide_response(*chest, state_truncated=True)

    with _decision_model(response=truncated), _llm():
        response = staff_client.post("/labels-lab/run/", _run_form(group))

    assert "did not fit" in response.content.decode()


@pytest.mark.django_db
def test_run_without_the_baseline_leaves_the_llm_alone(staff_client: Client, chest):
    group, *_ = chest

    with _decision_model(response=_decide_response(*chest)), _llm_left_alone():
        response = staff_client.post("/labels-lab/run/", _run_form(group, run_baseline=None))

    [view] = response.context["group_views"]
    assert response.status_code == 200
    assert view.gate.llm is None
    assert view.gate.noul_value == "YES"


@pytest.mark.django_db
def test_run_reports_a_decision_model_failure_and_still_shows_the_llm(staff_client: Client, chest):
    group, *_ = chest
    error = DecisionModelError(
        'http://ollaya.test/api/decide answered 404: model "laya:xl" not found',
        status=404,
        code="MODEL_NOT_FOUND",
    )

    with _decision_model(error=error), _llm():
        response = staff_client.post("/labels-lab/run/", _run_form(group))

    [view] = response.context["group_views"]
    assert "MODEL_NOT_FOUND" in response.content.decode()
    assert view.gate.noul is None
    assert view.gate.llm == "YES"
    assert view.labels[0].llm == "PRESENT"


@pytest.mark.django_db
def test_run_reports_an_llm_failure_and_still_shows_the_decision_model(staff_client: Client, chest):
    group, *_ = chest
    llm_down = patch(
        "radis.labels_lab.baseline.LLMClient", side_effect=RuntimeError("LLM endpoint is down")
    )

    with _decision_model(response=_decide_response(*chest)), llm_down:
        response = staff_client.post("/labels-lab/run/", _run_form(group))

    [view] = response.context["group_views"]
    assert "LLM endpoint is down" in response.content.decode()
    assert view.gate.llm is None
    assert view.labels[0].choice == "LIKELY"


@pytest.mark.django_db
@pytest.mark.parametrize(
    "invalid",
    [{"text": "   "}, {"groups": None}, {"gate": "1.5"}, {"possible": "0.7", "likely": "0.6"}],
)
def test_run_with_invalid_input_asks_no_model(staff_client: Client, chest, invalid):
    group, *_ = chest

    with _decision_model(response=_decide_response(*chest)) as decision_model, _llm_left_alone():
        response = staff_client.post("/labels-lab/run/", _run_form(group, **invalid))

    assert response.status_code == 200
    assert decision_model.calls == []
    assert "group_views" not in response.context
    assert "alert-danger" in response.content.decode()


@pytest.mark.django_db
def test_remap_applies_new_thresholds_without_asking_any_model(staff_client: Client, chest):
    group, *_ = chest
    with _decision_model(response=_decide_response(*chest)), _llm():
        run = staff_client.post("/labels-lab/run/", _run_form(group))
    [before] = run.context["group_views"]

    with _decision_model() as decision_model, _llm_left_alone():
        response = staff_client.post(
            "/labels-lab/remap/",
            {"mapping": run.context["mapping_json"], **DEFAULT_THRESHOLDS, "gate": "0.95"},
        )

    [after] = response.context["group_views"]
    assert decision_model.calls == []
    assert (before.gate.noul_value, after.gate.noul_value) == ("YES", "NO")
    assert after.gate.noul == 0.9127
    assert after.gate.llm == "YES"
    assert [(lbl.llm, lbl.choice) for lbl in after.labels] == [
        ("PRESENT", "LIKELY"),
        ("ABSENT", "ABSENT"),
    ]


@pytest.mark.django_db
def test_remap_before_any_run_changes_nothing(staff_client: Client):
    response = staff_client.post("/labels-lab/remap/", DEFAULT_THRESHOLDS)

    assert response.status_code == 204


@pytest.mark.django_db
@pytest.mark.parametrize("mapping", ["not json", '{"groups": "nope"}', "[1, 2, 3]"])
def test_remap_rejects_a_mapping_it_cannot_read(staff_client: Client, mapping):
    response = staff_client.post("/labels-lab/remap/", {"mapping": mapping, **DEFAULT_THRESHOLDS})

    assert response.status_code == 400


@pytest.mark.django_db
def test_run_sends_the_llm_the_calls_the_pipeline_sends_for_an_unlabeled_report(
    staff_client: Client,
):
    chest = LabelGroupFactory.create(
        name="Chest pathology", gate_question="Does this report describe imaging of the chest?"
    )
    # Created out of alphabetical order: the pipeline sends a group's labels in the order
    # its query returns them, and the LLM's answers depend on that order.
    for name in ("pneumothorax", "pleural effusion", "rib fracture", "pneumonia"):
        LabelFactory.create(group=chest, name=name)
    abdomen = LabelGroupFactory.create(
        name="Acute abdomen", gate_question="Does this report describe imaging of the abdomen?"
    )
    LabelFactory.create(group=abdomen, name="bowel obstruction")
    LabelFactory.create(group=abdomen, name="appendicitis")
    LabelFactory.create(group=abdomen, name="retired finding", active=False)
    report = ReportFactory.create(body="Lungs are clear. No free air.")

    pipeline_llm = RecordingLLM()
    with patch("radis.labels.labeling.LLMClient", return_value=pipeline_llm):
        label_report(report.pk)

    lab_llm = RecordingLLM()
    with (
        patch("radis.labels_lab.baseline.LLMClient", return_value=lab_llm),
        _decision_model(response={"model": "laya:en", "answers": {}}),
    ):
        staff_client.post(
            "/labels-lab/run/",
            _run_form(chest, text=report.body, groups=[chest.id, abdomen.id]),
        )

    assert len(pipeline_llm.calls) == 3  # the gates, then the labels of each group
    assert lab_llm.calls == pipeline_llm.calls


@pytest.mark.django_db
def test_run_sends_the_text_as_it_was_posted(staff_client: Client, chest):
    group, *_ = chest
    text = "\n  Lungs are clear.\n\n"

    with _decision_model(response=_decide_response(*chest)) as decision_model, _llm():
        staff_client.post("/labels-lab/run/", _run_form(group, text=text))

    assert decision_model.calls[0]["state"] == text


@pytest.mark.django_db
def test_run_flags_every_answer_that_differs_from_the_llm(staff_client: Client, chest):
    group, *_ = chest

    # Both gate variants say YES and both label variants say LIKELY for pneumonia, all four
    # against the LLM. Pneumothorax is ABSENT for the LLM and the choice, which the noul's
    # ABSENT / UNMENTIONED agrees with.
    with _decision_model(response=_decide_response(*chest)), _llm(gate="NO"):
        response = staff_client.post("/labels-lab/run/", _run_form(group))

    assert response.content.decode().count("≠ LLM") == 4


@pytest.mark.django_db
def test_run_mutes_the_labels_of_a_column_whose_gate_answers_no(staff_client: Client, chest):
    group, *_ = chest

    # At 0.9 the choice gate (0.85) says NO and the noul gate (0.9127) says YES.
    with _decision_model(response=_decide_response(*chest)), _llm(gate="NO"):
        response = staff_client.post("/labels-lab/run/", _run_form(group, gate="0.9"))

    # The LLM column and the choice column, for each of the two labels.
    assert response.content.decode().count("opacity-50") == 4


@pytest.mark.django_db
def test_run_keeps_what_a_remap_needs_in_the_page(staff_client: Client, chest):
    group, *_ = chest

    with _decision_model(response=_decide_response(*chest)), _llm():
        response = staff_client.post("/labels-lab/run/", _run_form(group))

    content = response.content.decode()
    assert 'id="lab-mapping"' in content
    assert 'id="lab-mapped"' in content
    assert json.loads(response.context["mapping_json"])["llm_gates"] == {str(group.id): "YES"}


@pytest.mark.django_db
def test_mapped_results_show_probabilities_as_precisely_as_they_are_thresholded(
    staff_client: Client, chest
):
    group, pneumonia, _ = chest
    decided = _decide_response(*chest)
    decided["answers"][f"present:{pneumonia.id}"]["noul"] = 0.8499
    with _decision_model(response=decided), _llm():
        run = staff_client.post("/labels-lab/run/", _run_form(group))

    # The remap answers with the mapped results alone, without the raw response next to them.
    response = staff_client.post(
        "/labels-lab/remap/", {"mapping": run.context["mapping_json"], **DEFAULT_THRESHOLDS}
    )

    [view] = response.context["group_views"]
    assert view.labels[0].noul_value == "LIKELY"  # 0.8499 is below the 0.85 it rounds to
    assert "0.8499" in response.content.decode()


@pytest.mark.django_db
def test_mapped_results_name_the_thresholds_they_were_mapped_with(staff_client: Client, chest):
    group, *_ = chest
    with _decision_model(response=_decide_response(*chest)), _llm():
        run = staff_client.post("/labels-lab/run/", _run_form(group, gate="0.37"))
    assert "0.37" in run.content.decode()

    response = staff_client.post(
        "/labels-lab/remap/",
        {"mapping": run.context["mapping_json"], **DEFAULT_THRESHOLDS, "possible": "0.31"},
    )

    assert "0.31" in response.content.decode()


@pytest.mark.django_db
def test_remap_with_invalid_thresholds_reports_them_instead_of_mapping(staff_client: Client, chest):
    group, *_ = chest
    with _decision_model(response=_decide_response(*chest)), _llm():
        run = staff_client.post("/labels-lab/run/", _run_form(group))

    response = staff_client.post(
        "/labels-lab/remap/",
        {"mapping": run.context["mapping_json"], **DEFAULT_THRESHOLDS, "possible": "0.9"},
    )

    assert response.status_code == 200
    assert "group_views" not in response.context
    assert "alert-danger" in response.content.decode()
