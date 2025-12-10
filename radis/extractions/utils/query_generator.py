"""
Query Generator for Automated Query Creation from Extraction Fields

This module provides functionality to automatically generate search queries
from user-defined extraction fields using LLM with fallback strategies.
"""

import logging
import re
from string import Template
from typing import Iterable

from django.conf import settings

from radis.chats.utils.chat_client import ChatClient
from radis.extractions.models import OutputField
from radis.search.utils.query_parser import QueryParser

logger = logging.getLogger(__name__)


class QueryGenerator:
    """Generates search queries from extraction fields using LLM with fallbacks."""

    def __init__(self):
        """Initialize the query generator with an LLM client."""
        self.client = ChatClient()
        self.parser = QueryParser()

    def generate_from_fields(self, fields: Iterable[OutputField]) -> tuple[str, dict[str, any]]:
        """
        Generate a search query from extraction fields.

        Args:
            fields: Iterable of OutputField objects to generate query from

        Returns:
            Tuple of (query_string, metadata_dict)
            - query_string: The generated query
            - metadata: Dict with keys:
                - generation_method: "llm", "keyword_fallback", or "wildcard"
                - field_count: Number of fields processed
                - success: Boolean indicating if generation succeeded
                - error: Optional error message if failed
        """
        fields_list = list(fields)
        field_count = len(fields_list)

        metadata = {
            "field_count": field_count,
            "success": False,
            "generation_method": None,
            "error": None,
        }

        # Handle empty fields
        if field_count == 0:
            logger.warning("No fields provided for query generation, using wildcard")
            metadata["generation_method"] = "wildcard"
            metadata["success"] = True
            return "*", metadata

        # Try LLM generation first
        if settings.ENABLE_AUTO_QUERY_GENERATION:
            try:
                query = self._call_llm(fields_list)
                if query:
                    # Validate and fix the query
                    validated_query, fixes = self.validate_and_fix_query(query)
                    if validated_query:
                        logger.info(
                            f"Successfully generated query from {field_count} fields using LLM"
                        )
                        metadata["generation_method"] = "llm"
                        metadata["success"] = True
                        metadata["fixes_applied"] = len(fixes) > 0
                        return validated_query, metadata
                    else:
                        logger.warning("LLM generated invalid query, trying fallback")
                        metadata["error"] = "LLM generated invalid query"
            except Exception as e:
                logger.error(f"Error during LLM query generation: {e}", exc_info=True)
                metadata["error"] = str(e)

        # Fallback to keyword extraction
        try:
            query = self._extract_keywords_fallback(fields_list)
            validated_query, fixes = self.validate_and_fix_query(query)
            if validated_query:
                logger.info(f"Generated query from {field_count} fields using keyword fallback")
                metadata["generation_method"] = "keyword_fallback"
                metadata["success"] = True
                metadata["fixes_applied"] = len(fixes) > 0
                return validated_query, metadata
        except Exception as e:
            logger.error(f"Error during keyword fallback: {e}", exc_info=True)
            metadata["error"] = f"Keyword fallback failed: {e}"

        # Final fallback to wildcard
        logger.warning("All query generation methods failed, using wildcard")
        metadata["generation_method"] = "wildcard"
        metadata["success"] = True
        return "*", metadata

    def _call_llm(self, fields: list[OutputField]) -> str | None:
        """
        Call LLM to generate a query from extraction fields.

        Args:
            fields: List of OutputField objects

        Returns:
            Generated query string, or None if failed
        """
        # Format fields for the prompt
        fields_formatted = self._format_fields_for_prompt(fields)

        # Build the prompt from template
        prompt = Template(settings.QUERY_GENERATION_SYSTEM_PROMPT).substitute(
            fields=fields_formatted
        )

        try:
            # Call LLM with timeout
            response = self.client.chat([{"role": "user", "content": prompt}])

            if not response:
                logger.warning("LLM returned empty response")
                return None

            # Extract and clean the query
            query = self._extract_query_from_response(response)

            logger.debug(f"LLM generated query: {query}")
            return query

        except Exception as e:
            logger.error(f"LLM call failed: {e}")
            return None

    def _format_fields_for_prompt(self, fields: list[OutputField]) -> str:
        """
        Format extraction fields for inclusion in LLM prompt.

        Args:
            fields: List of OutputField objects

        Returns:
            Formatted string representation of fields
        """
        formatted_fields = []
        for field in fields:
            field_dict = {
                "name": field.name,
                "description": field.description,
                "type": field.get_output_type_display(),
            }
            formatted_fields.append(str(field_dict))

        return "\n".join(formatted_fields)

    def _extract_query_from_response(self, response: str) -> str:
        """
        Extract query from LLM response.

        The LLM might return the query with additional text or formatting.
        This method cleans and extracts just the query.

        Args:
            response: Raw LLM response

        Returns:
            Cleaned query string
        """
        # Remove common prefixes like "Query:", "Search:", etc.
        cleaned = re.sub(
            r"^(query|search|generated query|result):\s*", "", response.strip(), flags=re.IGNORECASE
        )

        # Remove quotes if the entire response is quoted
        if cleaned.startswith('"') and cleaned.endswith('"'):
            cleaned = cleaned[1:-1]
        elif cleaned.startswith("'") and cleaned.endswith("'"):
            cleaned = cleaned[1:-1]

        # Take only the first line (in case LLM added explanation)
        cleaned = cleaned.split("\n")[0].strip()

        return cleaned

    def _extract_keywords_fallback(self, fields: list[OutputField]) -> str:
        """
        Fallback method: Extract keywords from field names and descriptions.

        Creates a simple OR query from field names and key terms from descriptions.

        Args:
            fields: List of OutputField objects

        Returns:
            Simple keyword-based query string
        """
        keywords = set()

        for field in fields:
            # Add field name (split on underscores and camelCase)
            name_parts = re.split(r"[_\s]+|(?<=[a-z])(?=[A-Z])", field.name)
            keywords.update(part.lower() for part in name_parts if len(part) > 2)

            # Extract key terms from description
            if field.description:
                # Remove common words
                stopwords = {
                    "the",
                    "a",
                    "an",
                    "and",
                    "or",
                    "but",
                    "in",
                    "on",
                    "at",
                    "to",
                    "for",
                    "of",
                    "with",
                    "is",
                    "are",
                    "was",
                    "were",
                    "be",
                    "been",
                    "being",
                    "this",
                    "that",
                    "these",
                    "those",
                }

                # Split description into words
                desc_words = re.findall(r"\b[a-zA-Z]{3,}\b", field.description.lower())
                keywords.update(word for word in desc_words if word not in stopwords)

        # Limit to most relevant keywords (max 10)
        keywords_list = sorted(keywords)[:10]

        if not keywords_list:
            # If no keywords extracted, return wildcard
            return "*"

        # Join with OR operator
        return " OR ".join(keywords_list)

    def validate_and_fix_query(self, query: str) -> tuple[str, list[str]]:
        """
        Validate and fix a query using QueryParser.

        Args:
            query: Query string to validate

        Returns:
            Tuple of (fixed_query, list_of_fixes_applied)
            Returns ("", []) if query is invalid
        """
        if not query or not query.strip():
            return "", []

        try:
            query_node, fixes = self.parser.parse(query)

            if query_node is None:
                logger.warning(f"Query validation failed for: {query}")
                return "", []

            # If fixes were applied, unparse to get the fixed query
            if len(fixes) > 0:
                fixed_query = QueryParser.unparse(query_node)
                logger.debug(f"Applied {len(fixes)} fixes to query: {fixes}")
                return fixed_query, fixes

            return query, []

        except Exception as e:
            logger.error(f"Error validating query '{query}': {e}")
            return "", []
