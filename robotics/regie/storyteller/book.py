"""
book — import of a book: plain text or HTML → cleaned text → chapters of paragraphs.

No LLM and no library: the text cut here is what NAO says, word for word.
"""

import html.parser
import re

# A line alone that starts a chapter: "CHAPITRE IV", "Chapitre premier", "XII."
DEFAULT_CHAPTER_RULE = (r"^(?:(?:CHAPITRE|Chapitre|CHAPTER|Chapter)\s+\S.{0,80}"
                        r"|(?=[IVXL])(?:XL|L?X{0,3})(?:IX|IV|V?I{0,3})\.?)$")

_START = re.compile(r"^\*{3}\s*START OF (?:THE|THIS) PROJECT GUTENBERG.*$", re.M | re.I)
_END = re.compile(r"^(?:\*{3}\s*END OF (?:THE|THIS) PROJECT GUTENBERG|End of (?:the )?Project Gutenberg).*$",
                  re.M | re.I)
_BLOCK_TAGS = {"p", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "blockquote", "hr"}
_SKIPPED_TAGS = {"script", "style", "head", "sup", "nav", "table"}


class _HtmlText(html.parser.HTMLParser):

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIPPED_TAGS:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n\n")

    def handle_endtag(self, tag):
        if tag in _SKIPPED_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(re.sub(r"\s+", " ", data))


def html_to_text(content: str) -> str:
    parser = _HtmlText()
    parser.feed(content)
    parser.close()
    return "".join(parser.parts)


def decode(raw: bytes) -> str:
    """UTF-8 (with or without BOM), else Latin-1 (older Gutenberg files)."""
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def clean(content: str) -> str:
    """Text without HTML, Gutenberg header and licence, footnotes and _italics_ marks."""
    if re.search(r"<(?:!doctype html|html|body|p)\b", content[:5000], re.I):
        content = html_to_text(content)
    content = content.replace("\r\n", "\n").replace("\r", "\n").replace(" ", " ")
    start = _START.search(content)
    if start:
        content = content[start.end():]
    end = _END.search(content)
    if end:
        content = content[:end.start()]
    content = re.sub(r"\[(?:Note|Footnote|Illustration)[^\]]*\]", "", content, flags=re.I)
    content = re.sub(r"\[\d{1,3}\]", "", content)
    content = re.sub(r"(?<!\w)_([^_]+)_(?!\w)", r"\1", content)
    content = "\n".join(line.strip() for line in content.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", content).strip() + "\n"


def paragraphs(block: str) -> list[str]:
    """Blank-line separated paragraphs, wrapped lines joined."""
    found = (" ".join(p.split()) for p in re.split(r"\n\s*\n", block))
    # Separators ("* * *", "---") hold nothing to say.
    return [p for p in found if any(c.isalnum() for c in p)]


def chapters(text: str, rule: str = DEFAULT_CHAPTER_RULE) -> list[dict]:
    """[{"chapter": 1, "title": "...", "paragraphs": [...]}] from the cleaned text.

    The heading line gives the title and is not read.  Headings with nothing
    under them (a table of contents) are dropped, like what precedes the first
    one; a text without any heading is one chapter.
    """
    pattern = re.compile(rule)
    found: list[tuple[str, list[str]]] = []
    current: list[str] | None = None
    preamble: list[str] = []
    for line in text.split("\n"):
        if pattern.match(line.strip()):
            current = []
            found.append((line.strip(), current))
        else:
            (preamble if current is None else current).append(line)
    if not found:
        found = [("", preamble)]
    result = []
    for title, lines in found:
        body = paragraphs("\n".join(lines))
        # A table of contents leaves short title lines under its headings.
        if not body or (len(found) > 1 and sum(len(p) for p in body) < 200):
            continue
        result.append({"chapter": len(result) + 1, "title": title, "paragraphs": body})
    if not result:
        raise ValueError("no text found")
    return result
