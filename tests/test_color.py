from unittest.mock import Mock

from kirk.color import irc_to_ansi, name_to_rgb


def mock_terminal() -> Mock:
    term = Mock()
    term.normal = "<N>"
    term.red = "<RED>"
    term.bold = lambda text: f"<B>{text}</B>"
    return term


def test_name_to_rgb_valid_range():
    for name in ["nick", "üñíçödé", "a" * 100, "nick[test]|mobile"]:
        rgb = name_to_rgb(name)
        assert len(rgb) == 3
        assert all(isinstance(c, int) and 0 <= c <= 255 for c in rgb)


def test_name_to_rgb_deterministic_and_distinct():
    assert name_to_rgb("nick1") == name_to_rgb("nick1")
    assert name_to_rgb("nick1") != name_to_rgb("nick2")


def test_irc_to_ansi_plain_text():
    term = mock_terminal()
    assert irc_to_ansi("Hello world", term) == "Hello world" + term.normal


def test_irc_to_ansi_color_code_stripped():
    term = mock_terminal()
    result = irc_to_ansi("\x0304Red text\x03", term)

    assert "Red text" in result
    assert "\x03" not in result
    assert term.red in result


def test_irc_to_ansi_bold_wraps_text():
    term = mock_terminal()
    result = irc_to_ansi("\x02Bold text\x02", term)

    assert result == "<B>Bold text</B>" + term.normal
    assert "\x02" not in result


def test_irc_to_ansi_truncated_color_code():
    term = mock_terminal()
    # a trailing, unterminated color code should not crash the parser
    result = irc_to_ansi("Hello \x03", term)
    assert "Hello" in result


def test_irc_to_ansi_invalid_color_number():
    term = mock_terminal()
    # color number outside the 0-15 mIRC palette
    result = irc_to_ansi("\x03999Invalid\x03", term)
    assert "Invalid" in result
