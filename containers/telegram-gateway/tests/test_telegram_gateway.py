from __future__ import annotations

import importlib.util
import unittest
from html.parser import HTMLParser
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "telegram_gateway.py"
SPEC = importlib.util.spec_from_file_location("telegram_gateway_under_test", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"unable to load {MODULE_PATH}")
telegram_gateway = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(telegram_gateway)


class BalancedHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.stack: list[str] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(tag)
            return
        self.stack.pop()


class VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def assert_balanced(test: unittest.TestCase, value: str) -> None:
    parser = BalancedHtmlParser()
    parser.feed(value)
    parser.close()
    test.assertEqual(parser.errors, [])
    test.assertEqual(parser.stack, [])


def visible_text(value: str) -> str:
    parser = VisibleTextParser()
    parser.feed(value)
    parser.close()
    return "".join(parser.parts)


class TelegramGatewayHtmlSplitTest(unittest.TestCase):
    def test_split_reopens_tags_without_breaking_closing_tag(self) -> None:
        text = telegram_gateway.sanitize_telegram_html(f"<code>{'한' * 70}</code>")

        chunks = telegram_gateway.split_telegram_html(text, limit=40)

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 40 for chunk in chunks))
        for chunk in chunks:
            assert_balanced(self, chunk)
        self.assertEqual("".join(visible_text(chunk) for chunk in chunks), "한" * 70)

    def test_split_preserves_nested_tags_links_and_entities(self) -> None:
        text = telegram_gateway.sanitize_telegram_html(
            '<b>prefix <a href="https://example.test/?a=1&amp;b=2">link &amp; text</a> suffix</b>'
        )

        chunks = telegram_gateway.split_telegram_html(text, limit=60)

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 60 for chunk in chunks))
        for chunk in chunks:
            assert_balanced(self, chunk)
        self.assertEqual("".join(visible_text(chunk) for chunk in chunks), visible_text(text))

    def test_explicit_break_is_applied_before_sanitizing(self) -> None:
        text = "<b>first</b><!--telegram-message-break--><i>second</i>"

        chunks = telegram_gateway.telegram_html_chunks(text)

        self.assertEqual(chunks, ["<b>first</b>", "<i>second</i>"])

    def test_july_14_closing_tag_boundary_shape_is_safe(self) -> None:
        text = telegram_gateway.sanitize_telegram_html("A" * 4085 + "<code>001450</code> tail")

        chunks = telegram_gateway.split_telegram_html(text)

        self.assertEqual(len(chunks), 2)
        self.assertTrue(all(len(chunk) <= 4096 for chunk in chunks))
        for chunk in chunks:
            assert_balanced(self, chunk)


if __name__ == "__main__":
    unittest.main()
