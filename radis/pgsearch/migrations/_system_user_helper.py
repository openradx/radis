from django.conf import settings


def create_system_user_idempotent(user_model) -> None:
    username = settings.EMBEDDING_SYSTEM_USERNAME
    user, created = user_model.objects.get_or_create(
        username=username,
        defaults={"is_active": False, "password": "!"},
    )
