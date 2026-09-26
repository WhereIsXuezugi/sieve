"""Which language a video is in, from its title and description.

YouTube's feeds do not say, so this guesses: the writing system first
(Cyrillic, Arabic, Devanagari, CJK scripts…), then, for Latin script, the
language whose common short words appear most. A guess needs enough evidence:
too little text and it says "" (unknown), and a filter that cannot tell,
abstains. yt-dlp's own "language" field wins when present.
"""

from __future__ import annotations

import re

LANGUAGES = {
    "en": "English", "de": "German", "fr": "French", "es": "Spanish", "it": "Italian",
    "pt": "Portuguese", "nl": "Dutch", "pl": "Polish", "tr": "Turkish", "sv": "Swedish",
    "ru": "Russian", "uk": "Ukrainian", "ar": "Arabic", "hi": "Hindi", "ja": "Japanese",
    "ko": "Korean", "zh": "Chinese", "el": "Greek", "he": "Hebrew", "th": "Thai",
}

_SCRIPTS = [
    ("ja", re.compile(r"[\u3040-\u30ff]")),           # kana: Japanese
    ("ko", re.compile(r"[\uac00-\ud7af]")),
    ("zh", re.compile(r"[\u4e00-\u9fff]")),
    ("ar", re.compile(r"[\u0600-\u06ff]")),
    ("hi", re.compile(r"[\u0900-\u097f]")),
    ("el", re.compile(r"[\u0370-\u03ff]")),
    ("he", re.compile(r"[\u0590-\u05ff]")),
    ("th", re.compile(r"[\u0e00-\u0e7f]")),
    ("uk", re.compile(r"[ієїґ]", re.I)),              # letters Russian lacks
    ("ru", re.compile(r"[\u0400-\u04ff]")),
]

_WORDS = {
    "en": "the and of to in is you that it for with how this what are on your why",
    "de": "der die das und ist nicht mit ein eine ich wie auf für den zu von sie",
    "fr": "le la les et des un une est pour dans que qui pas sur avec comment vous",
    "es": "el la los las y de que en un una es por para con cómo qué del se",
    "it": "il lo la gli le e di che un una è per con come non del della sono",
    "pt": "o a os as e de que em um uma é para com como não do da são você",
    "nl": "de het een en van is niet dat op met hoe voor je zijn wat ook",
    "pl": "i w na z że się nie jak to jest do co od dla czy",
    "tr": "ve bir bu da de için ile nasıl ne çok mi ben",
    "sv": "och att det är en som på med för hur inte av jag",
}
_SETS = {code: set(words.split()) for code, words in _WORDS.items()}
_TOKEN = re.compile(r"[^\W\d_]+", re.U)


def detect(text: str, declared: str = "") -> str:
    if declared:
        return declared.split("-")[0].lower()
    text = text or ""
    for code, pattern in _SCRIPTS:
        if len(pattern.findall(text)) >= 3:
            return code
    words = [w.lower() for w in _TOKEN.findall(text)]
    if len(words) < 4:
        return ""
    hits = {code: sum(1 for w in words if w in s) for code, s in _SETS.items()}
    best = max(hits, key=hits.get)
    ranked = sorted(hits.values(), reverse=True)
    # Enough evidence, and clearly ahead of the runner-up.
    if ranked[0] >= 2 and ranked[0] >= 1.5 * ranked[1]:
        return best
    return ""
