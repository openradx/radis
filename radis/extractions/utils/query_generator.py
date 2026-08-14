"""
Query Generator for Automated Query Creation from Extraction Fields

This module provides functionality to automatically generate search queries
from user-defined extraction fields using an LLM.
"""

import logging
import re
from collections.abc import Iterable
from string import Template
from typing import Any

import openai
from django.conf import settings

from radis.core.utils.llm_client import LLMResponseError
from radis.core.utils.rate_limit import RateLimited
from radis.extractions.models import OutputField
from radis.search.utils.query_parser import QueryParser

logger = logging.getLogger(__name__)


class AsyncQueryGenerator:
    """Query generator that uses async LLM calls to create search queries from extraction fields."""

    def __init__(self):
        """Initialize the async query generator."""
        self.parser = QueryParser()

    async def generate_from_fields(
        self, fields: Iterable[OutputField]
    ) -> tuple[str | None, dict[str, Any]]:
        """
        Generate a search query from extraction output fields using LLM.

        Args:
            fields: Iterable of OutputField objects to generate query from

        Returns:
            Tuple of (query_string or None if generation failed, metadata_dict)
            The metadata dict contains:
              - field_count: Number of fields processed
              - success: Whether query generation succeeded
              - generation_method: "llm" if successful, None otherwise
              - error: Error message if generation failed
              - fixes_applied: Whether query fixes were applied (when successful)
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

        if not settings.ENABLE_AUTO_QUERY_GENERATION:
            metadata["error"] = "Query generation is disabled"
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
            except (LLMResponseError, RateLimited, openai.APIError) as e:
                logger.error(f"Error during async LLM query generation: {e}", exc_info=True)
                metadata["error"] = str(e)

        logger.warning(f"Query generation failed for {field_count} fields")
        metadata["error"] = metadata.get("error") or "Query generation failed"
        metadata["success"] = False
        return None, metadata

    async def _call_llm(self, fields: list[OutputField]) -> str | None:
        """
        Call the LLM to generate a query from fields.

        Args:
            fields: List of OutputField objects

        Returns:
            Generated query string, or None if the call failed
        """
        from radis.core.utils.llm_client import AsyncChatClient

        fields_formatted = self._format_fields_for_prompt(fields)

        prompt = Template(settings.QUERY_GENERATION_SYSTEM_PROMPT).substitute(
            fields=fields_formatted
        )

        client = AsyncChatClient("query_generation", timeout=settings.QUERY_GENERATION_TIMEOUT)
        try:
            response = await client.chat(
                [{"role": "user", "content": prompt}],
                transient_retry_attempts=settings.QUERY_GENERATION_MAX_RETRIES,
            )

            if not response:
                logger.warning("LLM returned empty response")
                return None

            query = self._extract_query_from_response(response)
            logger.debug(f"Async LLM generated query: {query}")
            return query

        except (LLMResponseError, RateLimited, openai.APIError) as e:
            logger.error(f"Async LLM call failed: {e}")
            return None
        finally:
            await client.close()

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
            field_dict: dict[str, Any] = {
                "name": field.name,
                "description": field.description,
                "type": field.get_output_type_display(),
            }
            # For selection fields the options often carry the actual target
            # concepts (e.g. diagnoses), so they matter for query generation.
            if field.selection_options:
                field_dict["allowed_values"] = list(field.selection_options)
            if field.is_array:
                field_dict["multiple_values"] = True
            formatted_fields.append(str(field_dict))

        return "\n".join(formatted_fields)

    def _extract_query_from_response(self, response: str) -> str:
        """
        Extract query from LLM response.

        Args:
            response: Raw LLM response

        Returns:
            Cleaned query string
        """
        # Take the first line before unwrapping quotes, so trailing explanation
        # lines don't stop a quoted query from being unwrapped.
        cleaned = response.strip().split("\n")[0].strip()
        cleaned = re.sub(
            r"^(query|search|generated query|result):\s*", "", cleaned, flags=re.IGNORECASE
        )

        if cleaned.startswith('"') and cleaned.endswith('"'):
            cleaned = cleaned[1:-1]
        elif cleaned.startswith("'") and cleaned.endswith("'"):
            cleaned = cleaned[1:-1]

        return cleaned.strip()

    def validate_and_fix_query(self, query: str) -> tuple[str, list[str]]:
        """
        Validate and fix a query using QueryParser.

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

        except ValueError as e:
            logger.error(f"Error validating query '{query}': {e}")
            return "", []
