"""Unit tests for the AsyncQueryGenerator class."""

from unittest.mock import AsyncMock, patch

from django.test import TestCase, override_settings

from radis.extractions.models import OutputField, OutputType
from radis.extractions.utils.query_generator import AsyncQueryGenerator


class AsyncQueryGeneratorTest(TestCase):
    """Test cases for AsyncQueryGenerator."""

    def setUp(self):
        """Set up test fixtures."""
        self.generator = AsyncQueryGenerator()

    async def test_generate_from_empty_fields(self):
        """Test query generation with no fields returns None."""
        fields = []
        query, metadata = await self.generator.generate_from_fields(fields)

        assert query is None
        assert metadata["generation_method"] is None
        assert metadata["field_count"] == 0
        assert metadata["success"] is False
        assert metadata["error"] == "No fields provided"

    @patch("radis.chats.utils.chat_client.AsyncChatClient")
    async def test_llm_generation_success(self, mock_chat_client_class):
        """Test successful LLM query generation."""
        # Mock the LLM response using AsyncMock for async methods
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

        with override_settings(ENABLE_AUTO_QUERY_GENERATION=True):
            generator = AsyncQueryGenerator()  # Create new instance with mocked client
            query, metadata = await generator.generate_from_fields(fields)

        assert query != ""
        assert metadata["success"] is True
        assert query is not None
        assert "nodule" in query.lower()

    def test_validate_and_fix_valid_query(self):
        """Test validation of a valid query."""
        query = "lung AND nodule"
        fixed_query, fixes = self.generator.validate_and_fix_query(query)

        assert fixed_query == query
        assert len(fixes) == 0

    def test_validate_and_fix_empty_query(self):
        """Test validation of an empty query."""
        query = ""
        fixed_query, fixes = self.generator.validate_and_fix_query(query)

        assert fixed_query == ""
        assert len(fixes) == 0

    def test_validate_and_fix_query_with_quotes(self):
        """Test validation of a query with quoted phrases."""
        query = '"lung nodule" AND size'
        fixed_query, fixes = self.generator.validate_and_fix_query(query)

        assert fixed_query != ""
        assert "lung nodule" in fixed_query or "lung" in fixed_query

    def test_format_fields_for_prompt(self):
        """Test formatting of fields for LLM prompt."""
        fields = [
            OutputField(
                name="test_field",
                description="test description",
                output_type=OutputType.TEXT,
            )
        ]

        formatted = self.generator._format_fields_for_prompt(fields)

        assert "test_field" in formatted
        assert "test description" in formatted
        assert "Text" in formatted

    def test_extract_query_from_response_simple(self):
        """Test extraction of query from simple LLM response."""
        response = "lung AND nodule"
        query = self.generator._extract_query_from_response(response)

        assert query == "lung AND nodule"

    def test_extract_query_from_response_with_prefix(self):
        """Test extraction when LLM response has a prefix."""
        response = "Query: lung AND nodule"
        query = self.generator._extract_query_from_response(response)

        assert query == "lung AND nodule"

    def test_extract_query_from_response_with_quotes(self):
        """Test extraction when response is wrapped in quotes."""
        response = '"lung AND nodule"'
        query = self.generator._extract_query_from_response(response)

        assert query == "lung AND nodule"

    def test_extract_query_from_response_multiline(self):
        """Test extraction when LLM adds explanation on additional lines."""
        response = "lung AND nodule\n\nThis query will find..."
        query = self.generator._extract_query_from_response(response)

        # Should only take the first line
        assert query == "lung AND nodule"
        assert "This query" not in query
