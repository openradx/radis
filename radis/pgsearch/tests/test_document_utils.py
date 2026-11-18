from django.utils.safestring import SafeString

from radis.pgsearch.utils.document_utils import HEADLINE_START, HEADLINE_STOP, summary_markup


def _marked(word: str) -> str:
    return f"{HEADLINE_START}{word}{HEADLINE_STOP}"


def test_headline_markers_become_em_tags():
    out = summary_markup("any body", f"an {_marked('existing')} headline")
    assert out == "an <em>existing</em> headline"
    assert isinstance(out, SafeString)


def test_empty_headline_falls_back_to_the_body_head():
    body = " ".join(f"word{i}" for i in range(100))
    assert summary_markup(body, "", max_words=5) == "word0 word1 word2 word3 word4"


def test_short_body_is_returned_whole():
    assert summary_markup("only three words here", "", max_words=10) == "only three words here"


def test_body_head_is_escaped():
    out = summary_markup("<script>alert(1)</script> & more", "")
    assert out == "&lt;script&gt;alert(1)&lt;/script&gt; &amp; more"


def test_headline_text_is_escaped_around_intact_highlights():
    out = summary_markup("ignored", f"size <5mm {_marked('nodule')} & {_marked('mass')}")
    assert out == "size &lt;5mm <em>nodule</em> &amp; <em>mass</em>"


def test_a_literal_em_tag_in_the_report_stays_escaped():
    assert summary_markup("<em>not a highlight</em>", "") == "&lt;em&gt;not a highlight&lt;/em&gt;"
    assert (
        summary_markup("ignored", f"<em>x</em> {_marked('y')}")
        == "&lt;em&gt;x&lt;/em&gt; <em>y</em>"
    )


def test_stray_markers_are_dropped_not_rendered():
    assert summary_markup(f"a {HEADLINE_START}b c", "") == "a b c"
    assert summary_markup("ignored", f"a {HEADLINE_STOP}b {_marked('c')}") == "a b <em>c</em>"
    assert "<em>" not in summary_markup("ignored", f"{HEADLINE_START}unclosed")


def test_whitespace_in_the_body_head_is_collapsed():
    assert summary_markup("a\n\n  b\tc", "", max_words=3) == "a b c"
