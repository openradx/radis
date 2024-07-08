import pytest
from adit_radis_shared.accounts.factories import GroupFactory, UserFactory
from adit_radis_shared.accounts.models import User
from django.contrib.auth.models import Group


@pytest.fixture
def rag_group() -> Group:
    group = GroupFactory()
    # TODO: Add permissions to the group
    return group


@pytest.fixture
def user_with_group(rag_group) -> User:
    user = UserFactory()
    user.groups.add(rag_group)
    return user
