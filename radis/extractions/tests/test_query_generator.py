"""Unit tests for the AsyncQueryGenerator class."""

from unittest.mock import AsyncMock, patch

import pytest

from radis.extractions.models import OutputField, OutputType
from radis.extractions.utils.query_generator import AsyncQueryGenerator


@pytest.fixture
def generator() -> AsyncQueryGenerator:
    return AsyncQueryGenerator()


@pytest.mark.asyncio
async def test_generate_from_empty_fields(generator):
    """Query generation with no fields returns None."""
    query, metadata = await generator.generate_from_fields([])

    assert query is None
    assert metadata["generation_method"] is None
    assert metadata["field_count"] == 0
    assert metadata["success"] is False
    assert metadata["error"] == "No fields provided"


@pytest.mark.asyncio
@patch("radis.core.utils.llm_client.AsyncChatClient")
async def test_llm_generation_success(mock_chat_client_class, generator, settings):
    """Successful LLM query generation."""
    mock_client = AsyncMock()
    mock_client.chat.return_value = '("lung nodule" OR "pulmonary nodule") AND size'
    mock_chat_client_class.return_value = mock_client

    fields = [
        OutputField(
            name="nodule_size",
            description="size of lung nodule in millimeters",
            output_type=OutputType.NUMERIC,
        )
    ]

    settings.ENABLE_AUTO_QUERY_GENERATION = True
    query, metadata = await generator.generate_from_fields(fields)

    assert query != ""
    assert metadata["success"] is True
    assert query is not None
    assert "nodule" in query.lower()


def test_validate_and_fix_valid_query(generator):
    """Validation of a valid query."""
    query = "lung AND nodule"
    fixed_query, fixes = generator.validate_and_fix_query(query)

    assert fixed_query == query
    assert len(fixes) == 0


def test_validate_and_fix_empty_query(generator):
    """Validation of an empty query."""
    fixed_query, fixes = generator.validate_and_fix_query("")

    assert fixed_query == ""
    assert len(fixes) == 0


def test_validate_and_fix_query_with_quotes(generator):
    """Validation of a query with quoted phrases."""
    fixed_query, _fixes = generator.validate_and_fix_query('"lung nodule" AND size')

    assert fixed_query != ""
    assert "lung nodule" in fixed_query or "lung" in fixed_query


def test_format_fields_for_prompt(generator):
    """Formatting of fields for the LLM prompt."""
    fields = [
        OutputField(
            name="test_field",
            description="test description",
            output_type=OutputType.TEXT,
        )
    ]

    formatted = generator._format_fields_for_prompt(fields)

    assert "test_field" in formatted
    assert "test description" in formatted
    assert "Text" in formatted


def test_extract_query_from_response_simple(generator):
    """Extraction of query from a simple LLM response."""
    assert generator._extract_query_from_response("lung AND nodule") == "lung AND nodule"


def test_extract_query_from_response_with_prefix(generator):
    """Extraction when the LLM response has a prefix."""
    assert generator._extract_query_from_response("Query: lung AND nodule") == "lung AND nodule"


def test_extract_query_from_response_with_quotes(generator):
    """Extraction when the response is wrapped in quotes."""
    assert generator._extract_query_from_response('"lung AND nodule"') == "lung AND nodule"


def test_extract_query_from_response_multiline(generator):
    """Extraction when the LLM adds explanation on additional lines."""
    query = generator._extract_query_from_response("lung AND nodule\n\nThis query will find...")

    assert query == "lung AND nodule"
    assert "This query" not in query


def test_extract_query_from_response_quoted_first_line_with_explanation(generator):
    """A quoted query followed by explanation lines still gets its quotes unwrapped."""
    response = '"lung AND nodule"\nExplanation: this query targets lung findings.'

    assert generator._extract_query_from_response(response) == "lung AND nodule"


@pytest.mark.asyncio
async def test_generation_disabled_returns_error_metadata(generator, settings):
    """With auto generation disabled, no query is produced and metadata says why."""
    settings.ENABLE_AUTO_QUERY_GENERATION = False
    fields = [OutputField(name="f", description="d", output_type=OutputType.TEXT)]

    query, metadata = await generator.generate_from_fields(fields)

    assert query is None
    assert metadata["success"] is False
    assert metadata["error"] == "Query generation is disabled"


@pytest.mark.asyncio
@patch("radis.core.utils.llm_client.AsyncChatClient")
async def test_llm_error_is_reported_in_metadata(mock_chat_client_class, generator, settings):
    """An LLM failure surfaces as error metadata instead of an exception."""
    from radis.core.utils.llm_client import LLMResponseError

    mock_client = AsyncMock()
    mock_client.chat.side_effect = LLMResponseError("LLM returned no content")
    mock_chat_client_class.return_value = mock_client

    settings.ENABLE_AUTO_QUERY_GENERATION = True
    fields = [OutputField(name="f", description="d", output_type=OutputType.TEXT)]

    query, metadata = await generator.generate_from_fields(fields)

    assert query is None
    assert metadata["success"] is False
    assert metadata["error"] == "Query generation failed"


@pytest.mark.asyncio
@patch("radis.core.utils.llm_client.AsyncChatClient")
async def test_invalid_generated_query_is_reported(mock_chat_client_class, generator, settings):
    """An unparseable LLM query is rejected and reported via metadata."""
    mock_client = AsyncMock()
    mock_client.chat.return_value = "((((("
    mock_chat_client_class.return_value = mock_client

    settings.ENABLE_AUTO_QUERY_GENERATION = True
    fields = [OutputField(name="f", description="d", output_type=OutputType.TEXT)]

    query, metadata = await generator.generate_from_fields(fields)

    assert query is None
    assert metadata["success"] is False
    assert metadata["error"] == "LLM generated invalid query"
