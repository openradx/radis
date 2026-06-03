import pytest
from django.contrib.auth import get_user_model

User = get_user_model()


@pytest.mark.django_db
def test_system_user_exists_after_migrations():
    user = User.objects.get(username="system")
    assert user.is_active is False
    assert not user.has_usable_password()
