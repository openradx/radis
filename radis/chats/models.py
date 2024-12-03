import logging
from typing import Callable

from adit_radis_shared.common.models import AppSettings
from django.conf import settings
from django.db import models

logger = logging.getLogger(__name__)


class ChatsSettings(AppSettings):
    class Meta:
        verbose_name_plural = "Chats settings"


class Grammar(models.Model):
    name = models.CharField(max_length=100, unique=True)
    human_readable_name = models.CharField(max_length=100)
    grammar = models.TextField()
    llm_instruction = models.TextField()

    is_default = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.human_readable_name}"


class Chat(models.Model):
    title = models.CharField(max_length=255, default="New Chat")
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="chats"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    report = models.ForeignKey(
        "reports.Report", on_delete=models.CASCADE, related_name="chats", null=True
    )

    messages: models.QuerySet["ChatMessage"]

    def __str__(self):
        return f"Chat {self.pk}"


class ChatRole(models.TextChoices):
    SYSTEM = "S", "System"
    USER = "U", "User"
    ASSISTANT = "A", "Assistant"


class ChatMessage(models.Model):
    chat = models.ForeignKey(Chat, on_delete=models.CASCADE, related_name="messages")
    role = models.CharField(max_length=1, choices=ChatRole.choices)
    get_role_display: Callable[[], str]
    content = models.TextField()

    def __str__(self):
        return f"ChatMessage {self.pk}"
