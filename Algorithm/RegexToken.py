import re
from typing import FrozenSet


class EditionTokens:
    RE = re.compile(
        r'\b(?:'
        r'deluxe|expanded|extended|complete|collectors?|'
        r'special|bonus|limited|exclusive|definitive|ultimate|legacy|big one edition|remix|again|'
        r'tour|international|reissue|redux|standard|'
        r'explicit|clean|edited|'
        r'platinum|gold|diamond|'
        r'anniversary|remaster(?:ed)?|acoustic|unplugged|'
        r'side\s+[ab]|chapter\s+\w+|act\s+\w+|vol(?:ume)?\.?\s*\d+|'
        r'instrumental(?:s)?|acapella(?:s)?|'
        r'3am|scary\s+hours|hours\s+edition|taylor[\'’]s\s+version|'
        r'edition|version'
        r')\b'
        r'|'
        r'\b(?:'
        r'remix(?:ed)?|'
        r'radio\s+edit|'
        r'(?:extended|club|dub|original|vip|instrumental)\s+(?:mix|version)|'
        r'bootleg|rework|re-?edit'
        r')\b'
        r'|'
        r'(?:\w[\w\s]+\s)?edit(?=\s*[\)\]]|$)',
        re.IGNORECASE,
    )

    @classmethod
    def findall(cls, text: str) -> FrozenSet[str]:
        return frozenset(m.lower() for m in cls.RE.findall(text))

    @classmethod
    def has(cls, text: str) -> bool:
        return bool(cls.RE.search(text))


class CompilationTokens:
    WORDS = (
        "compilation", "hits", "playlist", "best of", "greatest", "power hits",
        "dj mix", "dj-mix", "mixed by", "the collection", "essential", "ultimate",
        "now that's", "super hits", "radio hits", "summer music", "beach party",
        "poolside", "party mix", "workout", "chill", "lounge", "relaxing",
    )
    RE = re.compile(
        r'\bdj[\s\-]?mix\b|\bmixed\b|\bselect\b|\bnow\s+that',
        re.IGNORECASE,
    )

    @classmethod
    def match(cls, text: str) -> bool:
        t = text.lower()
        return any(w in t for w in cls.WORDS) or bool(cls.RE.search(text))


class RemixTokens:
    RE = re.compile(
        r'\b(?:remix|remixed|edit|radio edit|extended mix|club mix|dub mix|original mix)\b',
        re.IGNORECASE,
    )

    @classmethod
    def has(cls, text: str) -> bool:
        return bool(cls.RE.search(text))


class FakeAlbumSuffix:
    RE = re.compile(
        r'\s*-\s*(single|ep|remix|edit|version|vip|mix|bootleg)\s*$',
        re.IGNORECASE,
    )

    @classmethod
    def strip(cls, text: str) -> str:
        return cls.RE.sub("", text).strip()

    @classmethod
    def has(cls, text: str) -> bool:
        return bool(cls.RE.search(text))