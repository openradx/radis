import asyncio
from typing import Callable, ContextManager
from unittest.mock import MagicMock

import nest_asyncio
import pytest

pytest_plugins = ["adit_radis_shared.pytest_fixtures"]


def pytest_configure():
    # pytest-asyncio doesn't play well with pytest-playwright as
    # pytest-playwright creates an event loop for the whole test suite and
    # pytest-asyncio can't create an additional one then.
    # nest_syncio works around this this by allowing to create nested loops.
    # https://github.com/pytest-dev/pytest-asyncio/issues/543
    # https://github.com/microsoft/playwright-pytest/issues/167
    nest_asyncio.apply()


@pytest.fixture
def openai_chat_completions_mock() -> Callable[[str], ContextManager]:
    def _openai_chat_completions_mock(content: str) -> ContextManager:
        mock_openai = MagicMock()
        mock_response = MagicMock(choices=[MagicMock(message=MagicMock(content=content))])
        future = asyncio.Future()
        future.set_result(mock_response)
        mock_openai.chat.completions.create.return_value = future

        return mock_openai

    return _openai_chat_completions_mock


@pytest.fixture
def default_grammars() -> None:
    from radis.core.management.commands.create_default_grammars import Command

    Command().handle()
