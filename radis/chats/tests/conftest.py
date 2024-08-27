import pytest
from faker import Faker


@pytest.fixture
def report_body() -> str:
    report_body = Faker().sentences(nb=40)
    return " ".join(report_body)


@pytest.fixture
def question_body() -> str:
    question_body = Faker().sentences(nb=1)
    return " ".join(question_body)
