"""Check that the text drawn on a slide matches the text its description asked for.

The image model draws letters as pictures and sometimes misspells or drops words. A text
model transcribes the finished slide without seeing the expected text (shown the expected
text, it tends to "read" what it expects and miss typos), and the two texts are compared
here, word by word.
"""

import difflib
import re
from dataclasses import dataclass

# «Текст на изображении: "…"; "…"» — the line of a 06 description with every caption of the slide.
_TEXT_LINE = re.compile(r"^[\s*_]*Текст на изображении[\s*_]*:[\s*_]*(.*)$", re.MULTILINE | re.IGNORECASE)
_WORD = re.compile(r"[^\W_]+")  # a run of letters and digits; punctuation and quotes are not compared
# Latin letters that look like Cyrillic ones (after lowercasing): the model may read «ОР» as "OP".
_LOOKALIKES = str.maketrans("aeopcxykmthb", "аеорсхукмтнв")


@dataclass(frozen=True)
class TextIssue:
    expected: str  # words that should be on the slide; "" if the slide has extra text
    found: str  # words read from the slide; "" if they are missing

    def describe(self) -> str:
        if not self.found:
            return f"пропущено «{self.expected}»"
        if not self.expected:
            return f"лишний текст «{self.found}»"
        return f"«{self.found}» вместо «{self.expected}»"


def expected_text(description: str) -> str:
    """Captions the slide must have, from the «Текст на изображении» line(s); "" if none."""
    return " ".join(m.group(1).strip() for m in _TEXT_LINE.finditer(description))


def compare(expected: str, found: str) -> list[TextIssue]:
    """Word-level differences between the expected captions and the text read from the slide.
    Case, punctuation, quotes, dashes and «ё»/«е» are ignored: they are not typos."""
    want, got = _words(expected), _words(found)
    want_keys, got_keys = [k for _, k in want], [k for _, k in got]
    issues = []
    matcher = difflib.SequenceMatcher(a=want_keys, b=got_keys, autojunk=False)
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "equal":
            continue
        # The same letters split or joined differently, e.g. a word broken across lines: not a typo.
        if "".join(want_keys[i1:i2]) == "".join(got_keys[j1:j2]):
            continue
        issues.append(
            TextIssue(
                expected=" ".join(w for w, _ in want[i1:i2]),
                found=" ".join(w for w, _ in got[j1:j2]),
            )
        )
    return _drop_moved(issues)


def confirmed(first: list[TextIssue], second: list[TextIssue]) -> list[TextIssue]:
    """Issues of the first reading that the second reading of the same slide found too.
    A real typo is read the same way twice; a misreading by the model usually is not."""
    seen = {_place(issue) for issue in second}
    return [issue for issue in first if _place(issue) in seen]


def _drop_moved(issues: list[TextIssue]) -> list[TextIssue]:
    """Captions read in a different order show up as the same words both missing and extra:
    the words are all on the slide, so it's not an error."""
    dropped: set[int] = set()
    for i, missing in enumerate(issues):
        if missing.found:
            continue
        for j, extra in enumerate(issues):
            if not extra.expected and j not in dropped and _key(extra.found) == _key(missing.expected):
                dropped.update((i, j))
                break
    return [issue for i, issue in enumerate(issues) if i not in dropped]


def _place(issue: TextIssue) -> tuple[str, str]:
    """Where on the slide the issue is: the expected words, or the extra words if none expected."""
    return ("expected", _key(issue.expected)) if issue.expected else ("extra", _key(issue.found))


def _key(text: str) -> str:
    return "".join(key for _, key in _words(text))


def _words(text: str) -> list[tuple[str, str]]:
    """(word as written, comparison key) pairs."""
    text = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", text)  # «кон-\nтроля» → «контроля»
    return [(w, w.lower().replace("ё", "е").translate(_LOOKALIKES)) for w in _WORD.findall(text)]
