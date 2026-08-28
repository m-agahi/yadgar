"""Task 425 — bare-scalar JSON transcript lines must not crash findings capture.

RCA: ``_parse_transcript_line`` called ``entry.get("message", entry)`` before
checking ``entry`` was a dict. A transcript line that parses as valid JSON but
whose value is a bare scalar (an int, a string) raised ``AttributeError``
instead of returning "" — the ``isinstance`` guard on the next line would have
caught it but ran one line too late. This crashed ``yadgar pending-findings``,
which stop-hook checkpoint step 4 depends on, for any transcript containing
such a line (measured live: 9/175 subagent .output transcripts in one session
had bare-scalar lines).
"""

from __future__ import annotations

from yadgar.core.hooks import findings_capture as fc


class TestParseTranscriptLineBareScalar:
    """_parse_transcript_line: valid-JSON-but-non-dict lines return "" instead of raising."""

    def test_bare_int_line_returns_empty(self):
        assert fc._parse_transcript_line("106") == ""

    def test_bare_str_line_returns_empty(self):
        assert fc._parse_transcript_line('"SELECT id, content FROM x"') == ""

    def test_bare_float_line_returns_empty(self):
        assert fc._parse_transcript_line("3.14") == ""

    def test_bare_bool_line_returns_empty(self):
        assert fc._parse_transcript_line("true") == ""

    def test_bare_null_line_returns_empty(self):
        assert fc._parse_transcript_line("null") == ""

    def test_bare_list_line_returns_empty(self):
        assert fc._parse_transcript_line("[1, 2, 3]") == ""

    def test_valid_assistant_dict_line_still_works(self):
        raw = '{"message": {"role": "assistant", "content": "hello"}}'
        assert fc._parse_transcript_line(raw) == "hello"

    def test_invalid_json_still_returns_empty(self):
        assert fc._parse_transcript_line("not json{") == ""


class TestLastAssistantTextMixedTranscript:
    """last_assistant_text: a transcript mixing good dict lines with bare-scalar
    lines must return the last assistant text, not raise."""

    def test_mixed_transcript_returns_last_assistant_text(self, tmp_path):
        lines = [
            '{"message": {"role": "user", "content": "do the thing"}}',
            "106",
            '"SELECT id, content FROM memory WHERE id = ?"',
            '{"message": {"role": "assistant", "content": "first reply"}}',
            "42",
            '{"message": {"role": "assistant", "content": "final reply"}}',
        ]
        p = tmp_path / "mixed.output"
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")

        assert fc.last_assistant_text(str(p)) == "final reply"

    def test_all_bare_scalar_transcript_returns_empty(self, tmp_path):
        lines = ["106", '"just a string"', "true", "null"]
        p = tmp_path / "allscalar.output"
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")

        assert fc.last_assistant_text(str(p)) == ""
