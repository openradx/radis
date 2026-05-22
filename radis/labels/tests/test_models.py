import pytest
from django.db import IntegrityError

from radis.labels.factories import QuestionFactory
from radis.labels.models import Question


class TestQuestion:
    def test_str_returns_label(self):
        q = QuestionFactory(label="pneumonia")
        assert str(q) == "pneumonia"

    def test_default_active_is_true(self):
        assert QuestionFactory().active is True

    def test_label_is_unique(self):
        QuestionFactory(label="pneumonia")
        with pytest.raises(IntegrityError):
            QuestionFactory(label="pneumonia")

    def test_updated_at_advances_on_save(self):
        q = QuestionFactory()
        before = q.updated_at
        q.text = "edited"
        q.save()
        q.refresh_from_db()
        assert q.updated_at > before
