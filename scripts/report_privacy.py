"""Remove computer labels from the rendered report without changing facts."""
from html import unescape
from html.parser import HTMLParser
from urllib.parse import unquote
import unicodedata


def computer_models(result, labels=()):
    model = result.get("hardware", {}).get("computer", {})
    names = {value for key in ("model", "matching_model") if isinstance(value := model.get(key), str) and value.strip()}
    normalise = lambda value: " ".join(unicodedata.normalize("NFKC",value).casefold().replace("_"," ").split())
    computer_labels = {normalise(value) for value in (*labels,"电脑","电脑型号","计算机","主机","主机型号","computer","computer model","host","host model","pc","mac")}
    additions = (result.get("user_input") or {}).get("device_models", {})
    for label,value in additions.items():
        if isinstance(label,str) and normalise(label) in computer_labels and isinstance(value,str) and value.strip():
            names.add(value)
    return names


def hide_computer_html(document, models):
    """Project already-calculated HTML; the same file is used for PDF export.

    Drop the containing evidence row, paragraph or link instead of replacing
    words in URLs or modifying the input used by ratings and integrity checks.
    """
    names = {value.casefold() for value in models if isinstance(value, str) and value.strip()}
    if not names:
        return document

    def private(value):
        decoded = unquote(unescape(value)).casefold()
        return any(name in decoded for name in names)

    class ReportParser(HTMLParser):
        void = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
        blocks = {"tr", "p", "li", "small", "a", "dt", "dd", "h1", "h2", "h3", "h4"}

        def __init__(self):
            super().__init__(convert_charrefs=False)
            self.frames = [["", "", [], False]]

        def handle_starttag(self, tag, attrs):
            opening = self.get_starttag_text()
            frame = [tag, opening, [], any(private(value) for _, value in attrs if value is not None)]
            if tag in self.void:
                if not frame[3]:
                    self.frames[-1][2].append(opening)
            else:
                self.frames.append(frame)

        def handle_endtag(self, tag):
            if len(self.frames) == 1 or self.frames[-1][0] != tag:
                raise ValueError("Unexpected report HTML structure")
            current, opening, children, hidden = self.frames.pop()
            content = "".join(children)
            if not hidden and not (current in self.blocks and private(content)):
                self.frames[-1][2].append(opening + content + "</" + tag + ">")

        def handle_startendtag(self, tag, attrs):
            if not any(private(value) for _, value in attrs if value is not None):
                self.frames[-1][2].append(self.get_starttag_text())

        def handle_data(self, data):
            self.frames[-1][2].append(data)

        def handle_entityref(self, name):
            self.handle_data("&" + name + ";")

        def handle_charref(self, name):
            self.handle_data("&#" + name + ";")

        def handle_decl(self, decl):
            self.handle_data("<!" + decl + ">")

    parser = ReportParser()
    parser.feed(document)
    parser.close()
    if len(parser.frames) != 1:
        raise ValueError("Incomplete report HTML structure")
    return "".join(parser.frames[0][2])
