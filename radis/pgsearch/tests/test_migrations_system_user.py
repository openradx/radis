import pytest
from django.contrib.auth import get_user_model

User = get_user_model()


@pytest.mark.django_db
def test_system_user_exists_after_migrations():
    user = User.objects.get(username="system")
    assert user.is_active is False
    assert not user.has_usable_password()


@pytest.mark.django_db
def test_creating_system_user_twice_is_a_noop():
    from radis.pgsearch.migrations import _system_user_helper

    before = User.objects.filter(username="system").count()
    _system_user_helper.create_system_user_idempotent(User)
    after = User.objects.filter(username="system").count()
    assert before == after == 1
