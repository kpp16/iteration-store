"""Anchor resolution — pure string operations, no fixtures needed."""

from __future__ import annotations

from iteration_store import anchors

PYTHON = '''\
import os


def alpha(x):
    return x + 1


class Beta:
    def method(self):
        return 2


def gamma():
    pass
'''

JAVASCRIPT = """\
const config = { debug: true };

export function handler(req, res) {
    if (req.ok) {
        res.send("yes");
    }
    return null;
}

const other = 1;
"""


class TestLineSpans:
    def test_resolves_explicit_span(self):
        assert anchors.resolve_anchor(PYTHON, "L4-L5") == (4, 5)

    def test_clamps_end_to_file_length(self):
        start, end = anchors.resolve_anchor(PYTHON, "L1-L9999")
        assert (start, end) == (1, len(PYTHON.splitlines()))

    def test_rejects_span_beyond_file(self):
        assert anchors.resolve_anchor(PYTHON, "L500-L600") is None

    def test_rejects_inverted_span(self):
        assert anchors.resolve_anchor(PYTHON, "L10-L2") is None


class TestSymbolSpans:
    def test_finds_function_and_its_body(self):
        span = anchors.resolve_anchor(PYTHON, "alpha")
        assert anchors.extract_span(PYTHON, span) == "def alpha(x):\n    return x + 1"

    def test_finds_class_including_nested_method(self):
        span = anchors.resolve_anchor(PYTHON, "Beta")
        body = anchors.extract_span(PYTHON, span)
        assert body.startswith("class Beta:")
        assert "return 2" in body
        assert "def gamma" not in body

    def test_finds_brace_delimited_function(self):
        span = anchors.resolve_anchor(JAVASCRIPT, "handler")
        body = anchors.extract_span(JAVASCRIPT, span)
        assert body.startswith("export function handler")
        assert body.rstrip().endswith("}")
        assert "const other" not in body

    def test_missing_symbol_returns_none(self):
        assert anchors.resolve_anchor(PYTHON, "nonexistent") is None

    def test_strips_path_prefix(self):
        with_prefix = anchors.resolve_anchor(PYTHON, "services/auth.py::alpha")
        assert with_prefix == anchors.resolve_anchor(PYTHON, "alpha")


class TestHashing:
    def test_is_stable_for_identical_text(self):
        assert anchors.hash_text("abc") == anchors.hash_text("abc")

    def test_differs_for_different_text(self):
        assert anchors.hash_text("abc") != anchors.hash_text("abd")
