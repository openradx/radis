import re
import unicodedata
from typing import Callable, Literal, cast

import pyparsing as pp


class TermNode:
    def __init__(self, term_type: Literal["WORD", "PHRASE"], value: str):
        self.term_type = term_type
        self.value = value

    def __repr__(self):
        return f"TermNode({self.term_type}, {self.value})"


class ParensNode:
    def __init__(self, expression: "QueryNode"):
        self.expression = expression

    def __repr__(self):
        return f"ParensNode({self.expression})"


class UnaryNode:
    def __init__(self, operator: Literal["NOT"], operand: "QueryNode"):
        self.operator = operator
        self.operand = operand

    def __repr__(self):
        return f"UnaryNode({self.operator}, {self.operand})"


class BinaryNode:
    def __init__(
        self,
        operator: Literal["AND", "OR"],
        left: "QueryNode",
        right: "QueryNode",
        implicit: bool = False,
    ):
        self.operator = operator
        self.left = left
        self.right = right

        if implicit and operator != "AND":
            raise ValueError("Implicit operator can only be an AND")

        self.implicit = implicit

    def __repr__(self):
        return f"BinaryNode({self.operator}, {self.left}, {self.right})"


QueryNode = TermNode | ParensNode | UnaryNode | BinaryNode


SAFE_TERM_CHARS = "_-'"


def is_search_token_char(char: str) -> bool:
    if char in SAFE_TERM_CHARS:
        return True
    category = unicodedata.category(char)
    return category[0] in ("L", "N", "M")


def is_search_query_char(char: str) -> bool:
    if char in "() ":
        return True
    return is_search_token_char(char)


class QueryParser:
    def __init__(self):
        pass

    def _replace_unbalanced_quotes(self, input_string: str) -> str:
        result = []
        in_quote = False  # Track if we are inside quotes
        last_unescaped_quote_index = -1

        i = 0
        while i < len(input_string):
            if input_string[i] == '"' and (
                i == 0 or input_string[i - 1] != "\\"
            ):  # Check for unescaped quote
                in_quote = not in_quote
                result.append(input_string[i])
                if in_quote:
                    last_unescaped_quote_index = i
            else:
                result.append(input_string[i])
            i += 1

        # If we are still inside a quote after processing, it means there is an unbalanced quote
        if in_quote and last_unescaped_quote_index != -1:
            result[last_unescaped_quote_index] = " "

        return "".join(result)

    def _replace_unbalanced_parentheses(self, input_string: str) -> str:
        stack = []
        result = list(input_string)
        inside_quotes = False
        escape_next = False

        for i, char in enumerate(input_string):
            if escape_next:
                escape_next = False
            elif char == "\\":
                escape_next = True
            elif char == '"' and not escape_next:
                inside_quotes = not inside_quotes
            if char == "(" and not inside_quotes:
                stack.append(i)
            elif char == ")" and not inside_quotes:
                if stack:
                    stack.pop()
                else:
                    result[i] = " "

        while stack:
            result[stack.pop()] = " "

        return "".join(result)

    def _modify_unquoted_segments(
        self, input_string: str, unquoted_segment_handler: Callable[[str], str]
    ) -> str:
        pattern = re.compile(r'(".*?(?<!\\)")')
        parts = pattern.split(input_string)

        results: list[str] = []
        for part in parts:
            if part.startswith('"'):
                assert part.endswith('"')
                results.append(part)
            else:
                results.append(unquoted_segment_handler(part))

        return "".join(results)

    def _strip_field_filters(self, input_string: str) -> str:
        """Drop `field:value` tokens (e.g., ``body:pneumonia``,
        ``patient_sex:F``, ``time:14:30``).

        The parser grammar has no field-filter syntax — structured field
        filtering lives on the provider side via ``SearchFilters``. Without
        this step the colon would be silently stripped by
        ``_replace_invalid_characters`` and ``body:pneumonia`` would collapse
        to ``bodypneumonia``, a meaningless token that pollutes both the FTS
        tsquery and the dense-embedding text. Drop the whole token instead.

        Operates only on unquoted segments so ``"body:pneumonia"`` inside a
        phrase is preserved verbatim.
        """
        pattern = re.compile(r"\b\w+:\S+")
        return self._modify_unquoted_segments(
            input_string, lambda s: pattern.sub("", s)
        )

    def _replace_invalid_characters(self, input_string: str) -> str:
        def handle_segment(segment: str) -> str:
            return "".join(char for char in segment if is_search_query_char(char))

        return self._modify_unquoted_segments(input_string, handle_segment)

    def _clean_consecutive_operators(self, input_string) -> str:
        pattern = r"((?:NOT|AND|OR)\b\s*)(?:(?:NOT|AND|OR)\b\s*)*(?:(?:AND|OR)\b\s*)+((?:NOT\b)*)"
        return self._modify_unquoted_segments(input_string, lambda s: re.sub(pattern, r"\1\2", s))

    def _clean_binary_operators_at_start_of_line(self, input_string: str) -> str:
        pattern = r"^(\s*(AND|OR))+\s*"
        cleaned_string = re.sub(pattern, r"", input_string)
        return cleaned_string

    def _clean_operators_at_end_of_line(self, input_string: str) -> str:
        pattern = r"(\s*(NOT|AND|OR))+\s*$"
        cleaned_string = re.sub(pattern, r"", input_string)
        return cleaned_string

    def _clean_binary_operators_at_start_of_parens(self, input_string: str) -> str:
        pattern = r"\((\s*(AND|OR))+\s*"
        return self._modify_unquoted_segments(input_string, lambda s: re.sub(pattern, r"(", s))

    def _clean_operators_at_end_of_parens(self, input_string: str) -> str:
        pattern = r"(\s*(NOT|AND|OR))+\s*\)"
        return self._modify_unquoted_segments(input_string, lambda s: re.sub(pattern, r")", s))

    def _clean_empty_parens(self, input_string: str) -> str:
        pattern = r"\(\s*\)"
        return self._modify_unquoted_segments(input_string, lambda s: re.sub(pattern, r"", s))

    def _strip_trailing_spaces(self, input_string: str) -> str:
        cleaned_string = input_string.strip()
        return cleaned_string

    def _delete_spaces_at_start_of_parens(self, input_string: str):
        return self._modify_unquoted_segments(input_string, lambda s: re.sub(r"\(\s*", "(", s))

    def _delete_spaces_at_end_of_parens(self, input_string: str) -> str:
        return self._modify_unquoted_segments(input_string, lambda s: re.sub(r"\s*\)", ")", s))

    def _reduce_spaces(self, input_string: str) -> str:
        return self._modify_unquoted_segments(input_string, lambda s: re.sub(r"\s+", " ", s))

    def _parse_string(self, input_string: str) -> QueryNode:
        not_ = pp.Keyword("NOT")
        and_ = pp.Keyword("AND")
        or_ = pp.Keyword("OR")
        lparen = pp.Literal("(")
        rparen = pp.Literal(")")

        # Input is pre-sanitized; keep regex broad to avoid pyparsing Unicode limitations.
        word = ~(not_ | and_ | or_) + pp.Regex(r"[^\s()]+").set_parse_action(
            lambda t: TermNode("WORD", t[0])  # type: ignore
        )
        phrase = pp.QuotedString(quoteChar='"', esc_char="\\").set_parse_action(
            lambda t: TermNode("PHRASE", t[0])  # type: ignore
        )
        term = phrase | word

        or_expression = pp.Forward()

        parens_expression = pp.Forward()
        parens_expression <<= (
            pp.Suppress(lparen) + or_expression + pp.Suppress(rparen)
        ).set_parse_action(lambda t: ParensNode(t[0])) | term  # type: ignore

        not_expression = pp.Forward()
        not_expression <<= (not_ + not_expression).set_parse_action(
            lambda t: UnaryNode("NOT", t[1])  # type: ignore
        ) | parens_expression

        and_expression = pp.Forward()
        and_expression <<= (
            (not_expression + and_ + and_expression).set_parse_action(
                lambda t: BinaryNode("AND", t[0], t[2])  # type: ignore
            )
            | (not_expression + and_expression).set_parse_action(
                lambda t: BinaryNode("AND", t[0], t[1], implicit=True)  # type: ignore
            )
            | not_expression
        )

        or_expression <<= (and_expression + or_ + or_expression).set_parse_action(
            lambda t: BinaryNode("OR", t[0], t[2])  # type: ignore
        ) | and_expression

        return cast(QueryNode, or_expression.parse_string(input_string, parse_all=True)[0])

    def parse(self, query: str) -> tuple[QueryNode | None, list[str]]:
        fixes: list[str] = []

        query_before = query
        query_after = self._replace_unbalanced_quotes(query_before)
        if query_before != query_after:
            fixes.append("Fixed unbalanced quotes")

        query_before = query_after
        query_after = self._replace_unbalanced_parentheses(query_before)
        if query_before != query_after:
            fixes.append("Fixed unbalanced parentheses")

        query_before = query_after
        query_after = self._strip_field_filters(query_before)
        if query_before != query_after:
            fixes.append("Stripped field-filter syntax (use the filter widgets instead)")

        query_before = query_after
        query_after = self._replace_invalid_characters(query_before)
        if query_before != query_after:
            fixes.append("Fixed invalid characters")

        query_before = query_after
        query_after = self._clean_consecutive_operators(query_before)
        if query_before != query_after:
            fixes.append("Fixed invalid consecutive operators")

        query_before = query_after
        query_after = self._clean_binary_operators_at_start_of_line(query_before)
        if query_before != query_after:
            fixes.append("Fixed invalid operators at start of line")

        query_before = query_after
        query_after = self._clean_operators_at_end_of_line(query_before)
        if query_before != query_after:
            fixes.append("Fixed invalid operators at end of line")

        query_before = query_after
        query_after = self._clean_binary_operators_at_start_of_parens(query_before)
        if query_before != query_after:
            fixes.append("Fixed invalid operators at start of parentheses")

        query_before = query_after
        query_after = self._clean_operators_at_end_of_parens(query_before)
        if query_before != query_after:
            fixes.append("Fixed invalid operators at end of parentheses")

        query_before = query_after
        query_after = self._clean_empty_parens(query_before)
        if query_before != query_after:
            fixes.append("Fixed empty parentheses")

        query_after = self._strip_trailing_spaces(query_after)
        query_after = self._delete_spaces_at_start_of_parens(query_after)
        query_after = self._delete_spaces_at_end_of_parens(query_after)
        query_after = self._reduce_spaces(query_after)

        if query_after == "":
            return None, fixes

        node = self._parse_string(query_after)
        return node, fixes

    @staticmethod
    def unparse(node: QueryNode) -> str:
        if isinstance(node, TermNode):
            if node.term_type == "WORD":
                return node.value
            elif node.term_type == "PHRASE":
                return f'"{node.value.replace('"', '\\"')}"'
            else:
                raise ValueError(f"Unknown term type: {node.term_type}")
        elif isinstance(node, ParensNode):
            return f"({QueryParser.unparse(node.expression)})"
        elif isinstance(node, UnaryNode):
            return f"{node.operator} {QueryParser.unparse(node.operand)}"
        elif isinstance(node, BinaryNode):
            if node.implicit:
                return f"{QueryParser.unparse(node.left)} {QueryParser.unparse(node.right)}"
            return (
                f"{QueryParser.unparse(node.left)} {node.operator} "
                + f"{QueryParser.unparse(node.right)}"
            )
        else:
            raise ValueError(f"Unknown node type: {type(node)}")

    @staticmethod
    def unparse_for_embedding(node: QueryNode) -> str:
        """Render the query as a plain bag of terms suitable for a dense
        embedding model.

        - Drops every ``UnaryNode("NOT", X)`` (embeddings are polarity-blind
          for negation; see spec §7.8).
        - Drops boolean operator tokens (``AND``/``OR``): they're query syntax,
          not content. The embedding model would otherwise see them as
          stopword-ish tokens cluttering the input.
        - Drops grouping parentheses for the same reason.
        - Drops quotes around phrases — embedding tokenizers handle multi-word
          spans natively; the literal quote chars only add noise.

        Returns the empty string if the whole query reduces to NOT clauses.
        Used by the hybrid-search vector half via ``providers.search``.
        """
        if isinstance(node, TermNode):
            # Emit the raw value for both WORD and PHRASE — no surrounding
            # quotes, since the embedding model doesn't care about them.
            return node.value
        if isinstance(node, ParensNode):
            return QueryParser.unparse_for_embedding(node.expression)
        if isinstance(node, UnaryNode):
            return ""
        if isinstance(node, BinaryNode):
            left = QueryParser.unparse_for_embedding(node.left)
            right = QueryParser.unparse_for_embedding(node.right)
            if not left and not right:
                return ""
            if not left:
                return right
            if not right:
                return left
            # Always join with a single space — operator tokens are dropped.
            return f"{left} {right}"
        raise ValueError(f"Unknown node type: {type(node)}")
