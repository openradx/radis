from radis.labels.query import extract_label_filters


def test_extracts_single_label_and_strips_it():
    remaining, labels = extract_label_filters("pneumonia label:edema")
    assert labels == ["edema"]
    assert "label:" not in remaining
    assert remaining.strip() == "pneumonia"


def test_extracts_multiple_labels():
    remaining, labels = extract_label_filters("label:edema chest label:nodule")
    assert labels == ["edema", "nodule"]
    assert remaining.strip() == "chest"


def test_quoted_label_allows_spaces():
    remaining, labels = extract_label_filters('label:"pleural effusion" lung')
    assert labels == ["pleural effusion"]
    assert remaining.strip() == "lung"


def test_no_label_returns_query_unchanged():
    remaining, labels = extract_label_filters("just a normal query")
    assert labels == []
    assert remaining == "just a normal query"


def test_label_token_is_case_preserved_for_name_match():
    _, labels = extract_label_filters("label:Pneumonia")
    assert labels == ["Pneumonia"]
