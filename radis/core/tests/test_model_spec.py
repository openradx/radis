import pytest

from radis.core.utils.model_spec import ModelSpecError, parse_model_spec


def test_a_bare_model_has_no_params():
    spec = parse_model_spec("qwen3.5:0.8b")

    assert spec.model == "qwen3.5:0.8b"
    assert spec.params == {}


def test_surrounding_whitespace_is_ignored():
    assert parse_model_spec("  qwen3.5:0.8b  ").model == "qwen3.5:0.8b"


def test_params_are_read_from_the_query_string():
    spec = parse_model_spec("qwen3.5:0.8b?reasoning_effort=none")

    assert spec.model == "qwen3.5:0.8b"
    assert spec.params == {"reasoning_effort": "none"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # JSON where it parses, so numbers and booleans reach the provider as such...
        ("temperature=0", {"temperature": 0}),
        ("top_p=0.9", {"top_p": 0.9}),
        ("enable_thinking=false", {"enable_thinking": False}),
        # ...including the capitalised spellings JSON does not know, which would
        # otherwise survive as the *truthy* strings "True"/"False".
        ("enable_thinking=False", {"enable_thinking": False}),
        ("enable_thinking=True", {"enable_thinking": True}),
        ("enable_thinking=FALSE", {"enable_thinking": False}),
        # ...and a plain string otherwise. "none" is a value providers expect literally,
        # so it must not become null.
        ("reasoning_effort=none", {"reasoning_effort": "none"}),
        ("reasoning_effort=low", {"reasoning_effort": "low"}),
    ],
)
def test_values_are_coerced_to_the_type_they_look_like(raw: str, expected: dict):
    assert parse_model_spec(f"a-model?{raw}").params == expected


def test_several_params_are_combined():
    spec = parse_model_spec("gpt-5?reasoning_effort=low&top_p=0.9&seed=42")

    assert spec.params == {"reasoning_effort": "low", "top_p": 0.9, "seed": 42}


def test_dotted_keys_become_nested_objects():
    # vLLM and SGLang take their template arguments nested like this.
    spec = parse_model_spec("qwen3:8b?chat_template_kwargs.enable_thinking=false")

    assert spec.params == {"chat_template_kwargs": {"enable_thinking": False}}


def test_dotted_keys_sharing_a_parent_are_merged():
    spec = parse_model_spec("m?a.b=1&a.c=2")

    assert spec.params == {"a": {"b": 1, "c": 2}}


def test_deeply_nested_keys():
    assert parse_model_spec("m?a.b.c=true").params == {"a": {"b": {"c": True}}}


@pytest.mark.parametrize("raw", ["", "   ", "?reasoning_effort=none"])
def test_a_spec_without_a_model_is_rejected(raw: str):
    with pytest.raises(ModelSpecError):
        parse_model_spec(raw)


def test_nesting_under_a_plain_value_is_rejected():
    with pytest.raises(ModelSpecError, match="already set to a plain value"):
        parse_model_spec("m?a=1&a.b=2")


def test_overwriting_nested_parameters_with_a_plain_value_is_rejected():
    # The mirror image of the case above. Accepting it would drop 'a.b' silently, and a
    # dropped parameter is exactly what the startup parse is meant to catch.
    with pytest.raises(ModelSpecError, match="already holds nested parameters"):
        parse_model_spec("m?a.b=1&a=2")


# A parameter the provider rejects fails every single request, so these have to be caught
# at startup rather than becoming a 400 on each call.
@pytest.mark.parametrize(
    "raw",
    [
        "m?reasoning_effort",  # no '=' at all
        "m?reasoning_effort=",  # nothing after the '='
        "m?reasoning_effort=   ",  # whitespace only
    ],
)
def test_a_parameter_without_a_value_is_rejected(raw: str):
    with pytest.raises(ModelSpecError, match="has no value"):
        parse_model_spec(raw)


@pytest.mark.parametrize("raw", ["m?a.=1", "m?.a=1", "m?a..b=1"])
def test_a_parameter_name_with_an_empty_part_is_rejected(raw: str):
    with pytest.raises(ModelSpecError, match="empty part"):
        parse_model_spec(raw)


def test_a_trailing_question_mark_is_rejected():
    with pytest.raises(ModelSpecError, match="sets no parameters"):
        parse_model_spec("a-model?")
