from types import SimpleNamespace

from radis.labels.utils.prompts import render_gate_prompt, render_label_prompt


def _label(name: str = "pneumonia", description: str = "infection of the lung parenchyma"):
    return SimpleNamespace(name=name, description=description)


def test_label_prompt_substitutes_report_including_unicode():
    body = "Lungen frei. 腫瘤なし. Sin patología."
    rendered = render_label_prompt(body, [_label()])
    assert body in rendered


def test_label_prompt_teaches_all_five_buckets():
    rendered = render_label_prompt("x", [_label()])
    for bucket in ("PRESENT", "LIKELY", "POSSIBLE", "ABSENT", "UNMENTIONED"):
        assert bucket in rendered


def test_label_prompt_contains_every_label_name_and_description():
    """The description must be in the prompt text: many providers never show the model the
    response schema, so a definition that lives only there is not read at all."""
    labels = [
        _label("pneumonia", "infection of the lung parenchyma"),
        _label("pleural effusion", "fluid in the pleural space"),
    ]
    rendered = render_label_prompt("the lungs are clear", labels)
    assert "- pneumonia: infection of the lung parenchyma" in rendered
    assert "- pleural effusion: fluid in the pleural space" in rendered


def test_label_prompt_renders_a_multiline_description_on_one_line():
    labels = [_label("pneumonia", "infection of the lung.\r\n\n  - includes aspiration")]
    rendered = render_label_prompt("x", labels)
    assert "- pneumonia: infection of the lung. - includes aspiration" in rendered


def test_label_prompt_substitutes_all_placeholders():
    rendered = render_label_prompt("the lungs are clear", [_label()])
    assert "$report" not in rendered
    assert "$labels" not in rendered


def test_label_prompt_keeps_dollar_signs_in_report_and_description():
    rendered = render_label_prompt("costs $report $5", [_label("x", "over $labels $10")])
    assert "costs $report $5" in rendered
    assert "- x: over $labels $10" in rendered


def test_gate_prompt_substitutes_report_and_teaches_gate_values():
    rendered = render_gate_prompt("report body here")
    assert "report body here" in rendered
    for value in ("YES", "NO"):
        assert value in rendered
    assert "MAYBE" not in rendered
