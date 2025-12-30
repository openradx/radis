"""
Query Generator for Automated Query Creation from Extraction Fields

This module provides functionality to automatically generate search queries
from user-defined extraction fields using LLM with fallback strategies.
"""

import logging
import re
from string import Template
from typing import Any, Iterable

from django.conf import settings

from radis.extractions.models import OutputField
from radis.search.utils.query_parser import QueryParser

logger = logging.getLogger(__name__)


class AsyncQueryGenerator:
    """Async version of QueryGenerator for use in async views."""

    def __init__(self):
        """Initialize the async query generator with an async LLM client."""
        from radis.chats.utils.chat_client import AsyncChatClient

        self.client = AsyncChatClient()
        self.parser = QueryParser()

    async def generate_from_fields(
        self, fields: Iterable[OutputField]
    ) -> tuple[str | None, dict[str, Any]]:
        """
        Async version of generate_from_fields.

        Args:
            fields: Iterable of OutputField objects to generate query from

        Returns:
            Tuple of (query_string, metadata_dict)
            Same structure as synchronous version
        """
        fields_list = list(fields)
        field_count = len(fields_list)

        metadata = {
            "field_count": field_count,
            "success": False,
            "generation_method": None,
            "error": None,
        }

        if field_count == 0:
            logger.warning("No fields provided for async query generation")
            metadata["error"] = "No fields provided"
            return None, metadata

        if settings.ENABLE_AUTO_QUERY_GENERATION:
            try:
                query = await self._call_llm(fields_list)
                if query:
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
                        logger.warning("LLM generated invalid query")
                        metadata["error"] = "LLM generated invalid query"
            except Exception as e:
                logger.error(f"Error during async LLM query generation: {e}", exc_info=True)
                metadata["error"] = str(e)

        logger.warning(f"Async query generation failed for {field_count} fields")
        metadata["error"] = metadata.get("error") or "All generation methods failed"
        metadata["success"] = False
        return None, metadata

    async def _call_llm(self, fields: list[OutputField]) -> str | None:
        """
        Async version of _call_llm.

        Args:
            fields: List of OutputField objects

        Returns:
            Generated query string, or None if failed
        """
        fields_formatted = self._format_fields_for_prompt(fields)

        prompt = Template(settings.QUERY_GENERATION_SYSTEM_PROMPT).substitute(
            fields=fields_formatted
        )

        try:
            response = await self.client.chat([{"role": "user", "content": prompt}])

            if not response:
                logger.warning("LLM returned empty response")
                return None

            query = self._extract_query_from_response(response)
            logger.debug(f"Async LLM generated query: {query}")
            return query

        except Exception as e:
            logger.error(f"Async LLM call failed: {e}")
            return None

    def _format_fields_for_prompt(self, fields: list[OutputField]) -> str:
        """
        Format extraction fields for inclusion in LLM prompt.
        Reuses synchronous version logic.

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
        Reuses synchronous version logic.

        Args:
            response: Raw LLM response

        Returns:
            Cleaned query string
        """
        cleaned = re.sub(
            r"^(query|search|generated query|result):\s*", "", response.strip(), flags=re.IGNORECASE
        )

        if cleaned.startswith('"') and cleaned.endswith('"'):
            cleaned = cleaned[1:-1]
        elif cleaned.startswith("'") and cleaned.endswith("'"):
            cleaned = cleaned[1:-1]

        cleaned = cleaned.split("\n")[0].strip()
        return cleaned

    def validate_and_fix_query(self, query: str) -> tuple[str, list[str]]:
        """
        Validate and fix a query using QueryParser.
        Reuses synchronous version logic.

        Args:
            query: Query string to validate

        Returns:
            Tuple of (fixed_query, list_of_fixes_applied)
        """
        if not query or not query.strip():
            return "", []

        try:
            query_node, fixes = self.parser.parse(query)

            if query_node is None:
                logger.warning(f"Query validation failed for: {query}")
                return "", []

            if len(fixes) > 0:
                fixed_query = QueryParser.unparse(query_node)
                logger.debug(f"Applied {len(fixes)} fixes to query: {fixes}")
                return fixed_query, fixes

            return query, []

        except Exception as e:
            logger.error(f"Error validating query '{query}': {e}")
            return "", []
