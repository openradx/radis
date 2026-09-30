import json

import httpx2
import pytest
from django.test import override_settings

from radis.labels_lab.decision_client import DecisionClient, DecisionModelError

QUESTIONS = {
    "gate_noul:7": {"type": "noul", "instructions": "Does this report describe the chest?"},
}

# A complete /api/decide response as documented by Ollaya, native fields included.
DECIDE_RESPONSE = {
    "model": "laya:en",
    "answers": {"gate_noul:7": {"type": "noul", "noul": 0.9127}},
    "usage": {"input_tokens": 118, "output_tokens": 0},
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

lab_settings = override_settings(
    DECISION_MODEL_URL="http://ollaya.test:11435/api/decide",
    DECISION_MODEL_API_KEY="",
    DECISION_MODEL_REQUEST_TIMEOUT_SECONDS=5.0,
)


def _client(handler) -> tuple[DecisionClient, list[httpx2.Request]]:
    """A client whose HTTP calls are answered by `handler`; the requests it made are kept."""
    requests: list[httpx2.Request] = []

    def recording_handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return handler(request)

    http_client = httpx2.Client(transport=httpx2.MockTransport(recording_handler))
    return DecisionClient(http_client=http_client), requests


def _decided(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(200, json=DECIDE_RESPONSE)


@lab_settings
def test_decide_posts_the_state_and_questions_to_the_configured_endpoint():
    client, requests = _client(_decided)

    client.decide("Lungs are clear.", QUESTIONS, model="laya:latest")

    [request] = requests
    assert request.method == "POST"
    assert str(request.url) == "http://ollaya.test:11435/api/decide"
    assert json.loads(request.content) == {
        "model": "laya:latest",
        "state": "Lungs are clear.",
        "questions": QUESTIONS,
    }


@lab_settings
def test_decide_returns_the_response_untouched_next_to_the_request_it_sent():
    client, _ = _client(_decided)

    decision = client.decide("Lungs are clear.", QUESTIONS, model="laya:latest")

    assert decision.response == DECIDE_RESPONSE
    assert decision.request == {
        "model": "laya:latest",
        "state": "Lungs are clear.",
        "questions": QUESTIONS,
    }
    assert decision.elapsed_ms >= 0


@lab_settings
def test_extras_are_part_of_the_request_only_when_asked_for():
    client, requests = _client(_decided)

    client.decide("Lungs are clear.", QUESTIONS, model="laya:latest", extras=["laya"])
    client.decide("Lungs are clear.", QUESTIONS, model="laya:latest")

    with_extras, without_extras = (json.loads(request.content) for request in requests)
    assert with_extras["extras"] == ["laya"]
    assert "extras" not in without_extras


@lab_settings
def test_no_authorization_header_is_sent_without_an_api_key():
    client, requests = _client(_decided)

    client.decide("Lungs are clear.", QUESTIONS, model="laya:latest")

    assert "authorization" not in requests[0].headers


@lab_settings
@override_settings(DECISION_MODEL_API_KEY="s3cret")
def test_a_configured_api_key_is_sent_as_bearer_token():
    client, requests = _client(_decided)

    client.decide("Lungs are clear.", QUESTIONS, model="laya:latest")

    assert requests[0].headers["authorization"] == "Bearer s3cret"


@lab_settings
def test_an_error_response_raises_with_the_servers_code_and_message():
    client, _ = _client(
        lambda request: httpx2.Response(
            404,
            json={
                "error": 'model "laya:xl" not found, try pulling it first',
                "code": "MODEL_NOT_FOUND",
            },
        )
    )

    with pytest.raises(DecisionModelError) as raised:
        client.decide("Lungs are clear.", QUESTIONS, model="laya:xl")

    assert raised.value.status == 404
    assert raised.value.code == "MODEL_NOT_FOUND"
    assert 'model "laya:xl" not found' in str(raised.value)


@lab_settings
def test_an_error_response_that_is_not_json_still_raises_with_its_status():
    client, _ = _client(lambda request: httpx2.Response(502, text="<html>Bad Gateway</html>"))

    with pytest.raises(DecisionModelError) as raised:
        client.decide("Lungs are clear.", QUESTIONS, model="laya:latest")

    assert raised.value.status == 502
    assert raised.value.code is None


@lab_settings
@pytest.mark.parametrize(
    "transport_error", [httpx2.ConnectError, httpx2.ReadTimeout, httpx2.DecodingError]
)
def test_an_unreachable_endpoint_raises_naming_the_endpoint(transport_error):
    def unreachable(request: httpx2.Request) -> httpx2.Response:
        raise transport_error("no answer", request=request)

    client, _ = _client(unreachable)

    with pytest.raises(DecisionModelError) as raised:
        client.decide("Lungs are clear.", QUESTIONS, model="laya:latest")

    assert "http://ollaya.test:11435/api/decide" in str(raised.value)
    assert raised.value.status is None


@lab_settings
@pytest.mark.parametrize(
    "body",
    [{"model": "laya:en", "usage": {}}, {"answers": ["not", "a", "map"]}, ["not", "an", "object"]],
)
def test_a_success_response_without_an_answers_object_is_rejected(body):
    client, _ = _client(lambda request: httpx2.Response(200, json=body))

    with pytest.raises(DecisionModelError):
        client.decide("Lungs are clear.", QUESTIONS, model="laya:latest")


@lab_settings
def test_list_models_reads_the_names_from_the_models_endpoint_of_the_same_server():
    def models(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={
                "models": [
                    {
                        "name": "laya:en",
                        "description": "English decision model (ModernBERT-large).",
                        "release_date": "2026-09-23",
                    },
                    {
                        "name": "laya:latest",
                        "description": "Routes each request to laya:en or laya:multilingual.",
                        "release_date": "2026-09-30",
                    },
                ]
            },
        )

    client, requests = _client(models)

    assert client.list_models() == ["laya:en", "laya:latest"]
    assert requests[0].method == "GET"
    assert str(requests[0].url) == "http://ollaya.test:11435/v1/models"


@lab_settings
def test_list_models_raises_when_the_server_cannot_be_reached():
    def unreachable(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused", request=request)

    client, _ = _client(unreachable)

    with pytest.raises(DecisionModelError):
        client.list_models()


@lab_settings
@override_settings(DECISION_MODEL_URL="http://ollaya.test:port/api/decide")
def test_an_endpoint_url_that_cannot_be_parsed_raises_for_both_calls():
    client, _ = _client(_decided)

    with pytest.raises(DecisionModelError):
        client.decide("Lungs are clear.", QUESTIONS, model="laya:latest")
    with pytest.raises(DecisionModelError):
        client.list_models()


@lab_settings
@pytest.mark.parametrize("body", [{"object": "list", "data": []}, {"models": "laya:en"}, []])
def test_list_models_rejects_an_answer_without_a_list_of_models(body):
    client, _ = _client(lambda request: httpx2.Response(200, json=body))

    with pytest.raises(DecisionModelError):
        client.list_models()


@lab_settings
@override_settings(DECISION_MODEL_REQUEST_TIMEOUT_SECONDS=42.0)
def test_a_decision_may_take_as_long_as_configured_but_listing_models_may_not():
    def answer(request: httpx2.Request) -> httpx2.Response:
        body = {"models": []} if request.method == "GET" else DECIDE_RESPONSE
        return httpx2.Response(200, json=body)

    client, requests = _client(answer)

    client.decide("Lungs are clear.", QUESTIONS, model="laya:latest")
    client.list_models()

    decide_timeout, list_timeout = (request.extensions["timeout"]["read"] for request in requests)
    assert decide_timeout == 42.0
    assert list_timeout < 42.0  # the lab page waits for this list before it renders
