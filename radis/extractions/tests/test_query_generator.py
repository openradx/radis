"""Unit tests for the QueryGenerator class."""

from unittest.mock import Mock, patch

from django.test import TestCase, override_settings

from radis.extractions.models import OutputField, OutputType
from radis.extractions.utils.query_generator import QueryGenerator


class QueryGeneratorTest(TestCase):
    """Test cases for QueryGenerator."""

    def setUp(self):
        """Set up test fixtures."""
        self.generator = QueryGenerator()

    def test_generate_from_empty_fields(self):
        """Test query generation with no fields returns wildcard."""
        fields = []
        query, metadata = self.generator.generate_from_fields(fields)

        assert query == "*"
        assert metadata["generation_method"] == "wildcard"
        assert metadata["field_count"] == 0
        assert metadata["success"] is True

    def test_keyword_fallback_single_field(self):
        """Test keyword extraction fallback with a single field."""
        field = OutputField(
            name="lung_nodule",
            description="size of lung nodule in millimeters",
            output_type=OutputType.NUMERIC,
        )

        with override_settings(ENABLE_AUTO_QUERY_GENERATION=False):
            query, metadata = self.generator.generate_from_fields([field])

        assert query != ""
        assert metadata["generation_method"] == "keyword_fallback"
        assert metadata["success"] is True
        assert metadata["field_count"] == 1
        # Should contain keywords from name and description
        assert "lung" in query.lower() or "nodule" in query.lower()

    def test_keyword_fallback_multiple_fields(self):
        """Test keyword extraction with multiple fields."""
        fields = [
            OutputField(
                name="fracture_type",
                description="type of bone fracture",
                output_type=OutputType.TEXT,
            ),
            OutputField(
                name="bone_name",
                description="which bone is fractured",
                output_type=OutputType.TEXT,
            ),
        ]

        with override_settings(ENABLE_AUTO_QUERY_GENERATION=False):
            query, metadata = self.generator.generate_from_fields(fields)

        assert query != ""
        assert metadata["generation_method"] == "keyword_fallback"
        assert metadata["success"] is True
        assert metadata["field_count"] == 2
        # Should use OR operator
        assert " OR " in query

    @patch("radis.extractions.utils.query_generator.ChatClient")
    def test_llm_generation_success(self, mock_chat_client_class):
        """Test successful LLM query generation."""
        # Mock the LLM response
        mock_client = Mock()
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
            generator = QueryGenerator()  # Create new instance with mocked client
            query, metadata = generator.generate_from_fields(fields)

        assert query != ""
        assert metadata["generation_method"] == "llm"
        assert metadata["success"] is True
        assert "nodule" in query.lower()

    @patch("radis.extractions.utils.query_generator.ChatClient")
    def test_llm_generation_failure_fallback(self, mock_chat_client_class):
        """Test that LLM failure triggers keyword fallback."""
        # Mock LLM to raise an exception
        mock_client = Mock()
        mock_client.chat.side_effect = Exception("LLM service unavailable")
        mock_chat_client_class.return_value = mock_client

        fields = [
            OutputField(
                name="tumor_size",
                description="size of the tumor in centimeters",
                output_type=OutputType.NUMERIC,
            )
        ]

        with override_settings(ENABLE_AUTO_QUERY_GENERATION=True):
            generator = QueryGenerator()
            query, metadata = generator.generate_from_fields(fields)

        # Should fall back to keyword extraction
        assert query != ""
        assert metadata["generation_method"] == "keyword_fallback"
        assert metadata["success"] is True
        assert metadata["error"] is not None

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

    def test_extract_keywords_from_camel_case(self):
        """Test keyword extraction from camelCase field names."""
        field = OutputField(
            name="patientAge",
            description="age of the patient in years",
            output_type=OutputType.NUMERIC,
        )

        with override_settings(ENABLE_AUTO_QUERY_GENERATION=False):
            query, metadata = self.generator.generate_from_fields([field])

        assert query != ""
        # Should split camelCase into separate words
        query_lower = query.lower()
        assert "patient" in query_lower or "age" in query_lower

    def test_extract_keywords_filters_stopwords(self):
        """Test that common stopwords are filtered from keyword extraction."""
        field = OutputField(
            name="finding",
            description="the finding in the report",
            output_type=OutputType.TEXT,
        )

        keywords = self.generator._extract_keywords_fallback([field])

        # Stopwords like "the", "in" should not be in the query
        assert "the" not in keywords.split()
        assert "in" not in keywords.split()

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

    def test_query_generation_with_boolean_field(self):
        """Test query generation with boolean output field."""
        field = OutputField(
            name="has_fracture",
            description="whether a fracture is present",
            output_type=OutputType.BOOLEAN,
        )

        with override_settings(ENABLE_AUTO_QUERY_GENERATION=False):
            query, metadata = self.generator.generate_from_fields([field])

        assert query != ""
        assert metadata["success"] is True
        assert "fracture" in query.lower()

    def test_query_generation_with_mixed_field_types(self):
        """Test query generation with multiple field types."""
        fields = [
            OutputField(
                name="nodule_present",
                description="is a nodule present",
                output_type=OutputType.BOOLEAN,
            ),
            OutputField(
                name="nodule_size",
                description="size in mm",
                output_type=OutputType.NUMERIC,
            ),
            OutputField(
                name="nodule_location",
                description="anatomical location",
                output_type=OutputType.TEXT,
            ),
        ]

        with override_settings(ENABLE_AUTO_QUERY_GENERATION=False):
            query, metadata = self.generator.generate_from_fields(fields)

        assert query != ""
        assert metadata["success"] is True
        assert metadata["field_count"] == 3
