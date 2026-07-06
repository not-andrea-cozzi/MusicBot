import re
from dataclasses import dataclass

class MusicPatterns:
    VERSION_TAG_RE = re.compile(
        r'[\(\[]\s*(?:remix|re-?mix|radio\s+edit|extended|vip|club\s+mix|'
        r'dub\s+mix|original\s+mix|acoustic|live|demo|instrumental)\b',
        re.IGNORECASE,
    )

    DELUXE_TAG_RE = re.compile(
        r'\b(?:deluxe|expanded|super\s+deluxe|anniversary|remastered|'
        r'special\s+edition|bonus\s+track)\b',
        re.IGNORECASE,
    )

    FEAT_RE = re.compile(r'\((?:feat|ft)\.?\s+([^)]+)\)', re.IGNORECASE)

    MULTI_ARTIST_SEP_RE = re.compile(r',|&', re.IGNORECASE)

    ALT_VERSION_RE = re.compile(
        r'\b(instrumental(?:s)?|karaoke|a\s*cappella(?:s)?|acapella(?:s)?|sped\s*up|'
        r'nightcore|slowed(?:\s*(?:and|&)?\s*reverb(?:ed)?)?|8d\s*audio|tiktok\s*remix)\b',
        re.IGNORECASE,
    )

    DURATION_TOLERANCE_MS = 5_000
    MATCHER_MIN_SCORE     = 0.55
    ITUNES_VALID_COUNTRIES = frozenset({
        "US","GB","CA","AU","NZ","IE","FR","DE","IT","ES","PT","NL",
        "BE","CH","AT","SE","NO","DK","FI","PL","RU","GR","TR","JP",
        "KR","CN","TW","HK","IN","SG","MY","ID","TH","PH","ZA","MX",
        "BR","AR","CL","CO","AE","SA","IL",
    })
    VARIOUS_ARTISTS = frozenset({"various artists", "aa.vv.", "artisti vari"})

    @staticmethod
    def normalize_artist_list(s: str) -> str:
        return re.sub(
            r"\s*(?:feat\.?|ft\.?|with|&|\+|and|;)\s*", ", ", s, flags=re.IGNORECASE
        ).strip(", ")

    @classmethod
    def is_alt_version(cls, text: str) -> bool:
        return bool(text) and bool(cls.ALT_VERSION_RE.search(text))