import time
from dataclasses import dataclass

import httpx2
from django.conf import settings

# The lab page asks for the model list before it renders, so a server that does not answer
# must not hold it up for as long as a decision may take.
_MODEL_LIST_TIMEOUT_SECONDS = 5.0


class DecisionModelError(Exception):
    """The decision-model endpoint could not be reached or did not return a decision."""

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status  # HTTP status, None when no response arrived
        self.code = code  # the server's machine-readable error code, if it sent one


@dataclass(frozen=True)
class Decision:
    request: dict
    response: dict
    elapsed_ms: float  # round trip as seen from here, network included


class DecisionClient:
    """Client for a System One decision endpoint.

    Built for Ollaya's native /api/decide, whose response adds routing, truncation and
    timings to the TypeSafe shape. A TypeSafe-compatible /v1/systemone endpoint takes the
    same request, so DECISION_MODEL_URL can point at one as well.
    """

    def __init__(self, http_client: httpx2.Client | None = None) -> None:
        self._url = settings.DECISION_MODEL_URL
        self._timeout = settings.DECISION_MODEL_REQUEST_TIMEOUT_SECONDS
        self._headers = {}
        if settings.DECISION_MODEL_API_KEY:
            self._headers["Authorization"] = f"Bearer {settings.DECISION_MODEL_API_KEY}"
        # Without a client to reuse, every request opens and closes its own connection.
        # The lab asks once per click, so there is no pool worth keeping open.
        self._request = http_client.request if http_client else httpx2.request

    def decide(
        self,
        state: str,
        questions: dict[str, dict],
        model: str,
        extras: list[str] | None = None,
    ) -> Decision:
        body: dict = {"model": model, "state": state, "questions": questions}
        if extras:
            body["extras"] = extras

        started = time.perf_counter()
        response = self._send("POST", self._url, self._timeout, json=body)
        elapsed_ms = (time.perf_counter() - started) * 1000

        data = self._json(response)
        if not isinstance(data, dict) or not isinstance(data.get("answers"), dict):
            raise DecisionModelError(
                f"{self._url} answered without an 'answers' object", status=response.status_code
            )
        return Decision(request=body, response=data, elapsed_ms=elapsed_ms)

    def list_models(self) -> list[str]:
        """The models the server offers, read from its TypeSafe-compatible model list."""
        try:
            url = str(httpx2.URL(self._url).join("/v1/models"))
        except httpx2.InvalidURL as err:
            raise DecisionModelError(f"{self._url!r} is not a URL: {err}") from err
        timeout = min(self._timeout, _MODEL_LIST_TIMEOUT_SECONDS)
        data = self._json(self._send("GET", url, timeout))
        models = data.get("models") if isinstance(data, dict) else None
        if not isinstance(models, list):
            raise DecisionModelError(f"{url} answered without a 'models' list")
        return [model["name"] for model in models if isinstance(model, dict) and "name" in model]

    def _send(self, method: str, url: str, timeout: float, **kwargs) -> httpx2.Response:
        try:
            response = self._request(method, url, headers=self._headers, timeout=timeout, **kwargs)
        except (httpx2.HTTPError, httpx2.InvalidURL) as err:
            raise DecisionModelError(
                f"The request to {url} failed: {type(err).__name__}: {err}"
            ) from err

        if response.is_error:
            error = self._json(response)
            error = error if isinstance(error, dict) else {}
            message = error.get("error") or response.text[:500] or response.reason_phrase
            code = error.get("code")
            raise DecisionModelError(
                f"{url} answered {response.status_code}: {message}",
                status=response.status_code,
                code=code if isinstance(code, str) else None,
            )
        return response

    @staticmethod
    def _json(response: httpx2.Response) -> object:
        try:
            return response.json()
        except ValueError:
            return None
