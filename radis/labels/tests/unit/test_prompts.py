import pytest

from radis.labels.utils.prompts import render_gate_prompt, render_label_prompt


def test_label_prompt_substitutes_report_including_unicode():
    body = "Lungen frei. 腫瘤なし. Sin patología."
    rendered = render_label_prompt(body)
    assert body in rendered


def test_label_prompt_teaches_all_five_buckets():
    rendered = render_label_prompt("x")
    for bucket in ("PRESENT", "LIKELY", "POSSIBLE", "ABSENT", "UNMENTIONED"):
        assert bucket in rendered


def test_gate_prompt_substitutes_report_and_teaches_gate_values():
    rendered = render_gate_prompt("report body here")
    assert "report body here" in rendered
    for value in ("YES", "NO", "MAYBE"):
        assert value in rendered


def test_prompts_contain_no_label_specific_text():
    """Per-label content belongs only in the schema field descriptions, never the prompt."""
    rendered = render_label_prompt("the lungs are clear")
    assert "pneumonia" not in rendered.lower()
    assert "$report" not in rendered  # placeholder must be fully substituted
