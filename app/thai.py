"""Helpers for Thai text pulled out of PDF files.

Many Thai PDFs (especially ones made from Microsoft Word with fonts like
Tahoma or Angsana) store "shifted" vowel/tone glyphs as Private Use Area
characters (U+F700-U+F71A). When that text is extracted, you get garbage
characters instead of real Thai vowels. This module maps them back to
standard Unicode and fixes common ordering problems.
"""

import re
import unicodedata

# Windows / Adobe Thai PUA glyph variants -> standard Thai code points
_PUA_MAP = {
    0xF700: "ฐ",  # ฐ without lower tail
    0xF701: "ิ",  # ิ shifted left
    0xF702: "ี",  # ี shifted left
    0xF703: "ึ",  # ึ shifted left
    0xF704: "ื",  # ื shifted left
    0xF705: "่",  # ่ shifted down-left
    0xF706: "้",  # ้ shifted down-left
    0xF707: "๊",  # ๊ shifted down-left
    0xF708: "๋",  # ๋ shifted down-left
    0xF709: "์",  # ์ shifted down-left
    0xF70A: "่",  # ่ shifted down
    0xF70B: "้",  # ้ shifted down
    0xF70C: "๊",  # ๊ shifted down
    0xF70D: "๋",  # ๋ shifted down
    0xF70E: "์",  # ์ shifted down
    0xF70F: "ญ",  # ญ without lower tail
    0xF710: "ั",  # ั shifted left
    0xF711: "ํ",  # ํ shifted left
    0xF712: "็",  # ็ shifted left
    0xF713: "่",  # ่ shifted left
    0xF714: "้",  # ้ shifted left
    0xF715: "๊",  # ๊ shifted left
    0xF716: "๋",  # ๋ shifted left
    0xF717: "์",  # ์ shifted left
    0xF718: "ุ",  # ุ shifted down
    0xF719: "ู",  # ู shifted down
    0xF71A: "ฺ",  # ฺ shifted down
}
_PUA_TABLE = str.maketrans(_PUA_MAP)

_TONES = "่้๊๋"
_UPPER_VOWELS = "ัิีึื็ํ"
_LOWER_VOWELS = "ฺุู"


def normalize_thai(text: str) -> str:
    """Return clean, standard-Unicode Thai text."""
    if not text:
        return text
    text = text.translate(_PUA_TABLE)
    text = unicodedata.normalize("NFC", text)
    # ํ + า  -> ำ   (also when a tone mark sits on either side of ํ)
    text = re.sub("([" + _TONES + "]?)ํ([" + _TONES + "]?)า",
                  lambda m: m.group(1) + m.group(2) + "ำ", text)
    # tone mark typed before an upper/lower vowel -> vowel first, then tone
    text = re.sub("([" + _TONES + "์])([" + _UPPER_VOWELS + _LOWER_VOWELS + "])",
                  r"\2\1", text)
    # duplicated combining marks (some PDFs draw marks twice for "bold")
    text = re.sub("([ัิ-ฺ็-๎])\\1+", r"\1", text)
    # Sara Am drawn as ำ but tone placed after it: น ำ ้ -> น ้ ำ
    text = re.sub("ำ([" + _TONES + "])", r"\1ำ", text)
    return text


def has_thai(text: str) -> bool:
    return any("฀" <= ch <= "๿" for ch in text)
