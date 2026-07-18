import re

from Algorithm.RegexToken import EditionTokens


class TextCleaner:
    _NOISE = re.compile(
        r"""
          \s*🎥[^\(\[]*(?=[\(\[]|$)
        | \s*[-–]?\s*\b(?:official\s+)?(?:audio|video|music\s+video|lyric\s+video|visualizer|clip)\b
        | \s*[\(\[]\b(?:official\s+)?(?:audio|video|music\s+video|lyric\s+video|visualizer|clip)\b[\)\]]
        | \s*[\(\[]?\b(?:prod\.?(?:\s*by)?|produced(?:\s+by)?)\b[^)\]]*[\)\]]?
        | \s*\(?\b(?:dir\.?\s*(?:by\s+)?[@\w][^)\]]*)\)?
        | \s*\(?\b(?:remaster(?:ed)?|extended|acoustic|live)\b[^)\]]*\)?
        | \s*\b(?:ft\.?|feat\.?|featuring|w/)(?:\b\s+|\s+).+?(?=\s*[\(\[]|$)
        | \s*[-–]\s*(?:ft\.?|feat\.?|featuring|w/)\s+.+$
        | \s*-\s*Topic\b.*$
        | \s*[\(\[]\b(?:
            explicit | clean | interlude | skit | intro | outro
        | instrumental | radio\s+edit | (?:album|single)\s+version
        | lyrics? | (?:hd\s+)?\d{3,4}p | hd | hq | 4k
        | slowed(?:\s*\+\s*reverb)? | sped\s+up | reverb
        | mixed | dj[\s\-]?mix | clean\s+version
        )\b[\)\]]
                """,
        re.IGNORECASE | re.VERBOSE,
    )

    _FEAT_PAREN        = re.compile(r'\s*[\(\[]\s*(?:ft\.?|feat\.?|featuring|w/)\s*[^\)\]]+[\)\]]', re.IGNORECASE)
    _FEAT_BEFORE_DASH  = re.compile(r'\s*[\(\[]?\s*\b(?:ft\.?|feat\.?|featuring|w/)\s*.+?(?=\s*[-–])', re.IGNORECASE)
    _COLLAB_PREFIX     = re.compile(r'^(.+?)\s+(?:x|&)\s+.+?\s*[-–]\s*', re.IGNORECASE)
    _TRAILING_NOISE_BRACKET = re.compile(
        r'\s*\[\s*(?:'
        r'official\s+(?:audio|video|music\s+video|lyric\s+video)'
        r'|audio|video|music\s+video|lyric\s+video|visualizer|clip'
        r'|(?:hd\s+)?\d{3,4}p|hd|hq|4k|explicit|clean|lyrics?|\d{4}'
        r'|mixed|dj[\s\-]?mix|clean\s+version'
        r')\s*\]\s*$',
        re.IGNORECASE,
    )
    _LABEL_BRACKET  = re.compile(r'\s*\[[\w\s&\.\-]+(?:records?|music|recordings?|entertainment|audio|releases?|group|label)\s*\]\s*$', re.IGNORECASE)
    _LABEL_CHANNEL  = re.compile(r'\b(?:records?|music|recordings?|entertainment|audio|releases?|group|label|official|vevo|tv)\b', re.IGNORECASE)
    _QUOTES         = re.compile(r'["""＂«»\u201c\u201d\uff02]')
    _VERSION_TAGS   = re.compile(r'\b(?:instrumental|karaoke|acapella|a\s*cappella|backing\s+track|minus\s+one)\b', re.IGNORECASE)
    _FEAT_INLINE    = re.compile(r'\s*[\(\[]\s*(?:feat\.?|ft\.?|featuring|with|w/)\s+[^)\]]+[\)\]]', re.IGNORECASE)
    _ALBUM_TITLE_NEGATIVE = re.compile(r"\b(?:live|concert|benefit|tour|unplugged|session|sessions|at\s+the|at\s+madison|at\s+glastonbury)\b", re.IGNORECASE)

    _STOPWORDS = frozenset({
        "a", "an", "the", "of", "in", "on", "at", "to", "for",
        "and", "or", "but", "with", "ft", "feat", "vs",
    })

    RELEASE_NEGATIVE = {"Compilation", "Live", "Remix", "Soundtrack", "DJ-mix", "Broadcast"}

    # Nomi d'arte con separatori (&, ',') che NON vanno spezzati in primary_artist.
    # Match esatto (case-insensitive, whole string) prima dello split standard.
    _KNOWN_MULTI_ARTISTS = frozenset({
        "takagi & ketra", "tyler, the creator", "earth, wind & fire",
    })

    @classmethod
    def normalize(cls, s: str) -> str:
        if not s:
            return ""
        import unicodedata
        s = re.sub(r"[''´`'']", "'", s)
        s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
        return s.lower().strip()

    @classmethod
    def primary_artist(cls, artist: str) -> str:
        if not artist:
            return ""
        al = artist.lower().strip()
        if al in cls._KNOWN_MULTI_ARTISTS:
            return artist.strip()
        return re.split(
            r'\s+(?:x|and)\s+|\s*&\s*|\s+ft\.?\s+|\s+feat\.?\s+|,\s+',
            artist, maxsplit=1, flags=re.IGNORECASE
        )[0].strip()

    @classmethod
    def clean_title(cls, title: str, artist: str = "") -> str:
        original = title
        title = cls._QUOTES.sub("", title).strip()

        if artist:
            title = cls._FEAT_BEFORE_DASH.sub("", title)
            art = cls.primary_artist(artist)
            m = cls._COLLAB_PREFIX.match(title)
            if m and m.group(1).lower().startswith(art.lower()):
                title = title[m.end():]
            else:
                art_words = [re.escape(w) for w in art.split()]
                if art_words:
                    art_pattern = r'\s+'.join(art_words)
                    title = re.sub(rf"^{art_pattern}\s*[-–—―_~:|]+\s*", "", title, flags=re.IGNORECASE)

        title = cls._FEAT_PAREN.sub("", title)
        title = cls._NOISE.sub("", title)
        title = cls._LABEL_BRACKET.sub("", title)
        title = re.sub(r'\s*[\(\[]\s*[\)\]]?\s*$', '', title).strip()
        title = re.sub(r'\s*[\(\[]\s*$', '', title).strip()
        title = re.sub(r'\s*[-–—―]\s*$', '', title).strip()
        title = re.sub(r'\s*[\(\[]\s*feat[^\)\]]*[\)\]]', '', title, flags=re.IGNORECASE)
        title = re.sub(r'(?i)$r$', '®', title)

        while True:
            prev = title
            title = cls._TRAILING_NOISE_BRACKET.sub("", title).strip()
            title = cls._LABEL_BRACKET.sub("", title).strip()
            if title == prev:
                break

        title = title.strip()
        if not title or (artist and title.lower() == cls.primary_artist(artist).lower()):
            return cls._QUOTES.sub("", original).strip()
        return title

    @staticmethod
    def sanitize_filename(name: str) -> str:
        if not name:
            return ""
        name = name.replace("\u2018", "'").replace("\u2019", "'")
        name = name.replace("\u201c", '"').replace("\u201d", '"')
        name = name.replace("？", "?")
        return re.sub(r'[\\/:*"<>|]', "_", name).strip()

    @classmethod
    def has_version_tag(cls, s: str) -> bool:
        return bool(cls._VERSION_TAGS.search(s))

    @classmethod
    def looks_like_label(cls, name: str) -> bool:
        return bool(cls._LABEL_CHANNEL.search(name))

    @classmethod
    def title_similarity(cls, a: str, b: str) -> float:
        if not a or not b:
            return 0.0

        al = cls.normalize(a)
        bl = cls.normalize(b)

        if al == bl:
            return 1.0

        try:
            from rapidfuzz import fuzz
            tsr = fuzz.token_set_ratio(al, bl) / 100.0
            r   = fuzz.ratio(al, bl) / 100.0
        except ImportError:
            wa = set(re.findall(r'\b\w+\b', al)) - cls._STOPWORDS
            wb = set(re.findall(r'\b\w+\b', bl)) - cls._STOPWORDS
            if not wa or not wb:
                return 0.0
            jaccard = len(wa & wb) / max(len(wa), len(wb))
            char_sim = sum(c in bl for c in al) / max(len(al), len(bl))
            if char_sim < 0.45:
                return 0.0
            return max(0.0, jaccard)

        if r < 0.45:
            return 0.0

        short_len = min(len(al), len(bl))
        if short_len <= 6:
            base = 0.15 * tsr + 0.85 * r
        elif short_len <= 12:
            base = 0.25 * tsr + 0.75 * r
        elif short_len <= 20:
            base = 0.45 * tsr + 0.55 * r
        else:
            base = 0.60 * tsr + 0.40 * r

        tokens_a = set(re.findall(r'\b\w+\b', al)) - cls._STOPWORDS
        tokens_b = set(re.findall(r'\b\w+\b', bl)) - cls._STOPWORDS
        if tokens_a and tokens_b:
            token_overlap = len(tokens_a & tokens_b) / max(len(tokens_a), len(tokens_b))
            if token_overlap == 1.0:
                base = min(1.0, base + 0.08)
            elif token_overlap == 0.0 and len(tokens_a) > 1:
                base -= 0.10

        nums_a = set(re.findall(r'\b(?:\d+|ii|iii|iv|v|vi|vii|viii|ix|x|pt\s*\d+)\b', al))
        nums_b = set(re.findall(r'\b(?:\d+|ii|iii|iv|v|vi|vii|viii|ix|x|pt\s*\d+)\b', bl))
        if nums_a != nums_b:
            base -= 0.40

        len_a, len_b = len(al), len(bl)
        if len_a > 0 and len_b > 0:
            len_ratio = min(len_a, len_b) / max(len_a, len_b)
            if len_ratio < 0.5:
                if al in bl or bl in al:
                    base *= 0.85 + 0.15 * len_ratio
                else:
                    base *= 0.5 + 0.5 * len_ratio

        return max(0.0, base)

    @classmethod
    def _edition_tokens(cls, s: str) -> frozenset:
        return frozenset(m.lower() for m in EditionTokens.findall(s))

    @classmethod
    def album_edition_similarity(cls, hint: str, candidate: str) -> float:
        base = cls.title_similarity(hint, candidate)
        if base == 0.0:
            return 0.0

        hint_ed = cls._edition_tokens(hint)
        cand_ed = cls._edition_tokens(candidate)

        if hint_ed == cand_ed:
            return base

        common = hint_ed & cand_ed
        diff   = hint_ed.symmetric_difference(cand_ed)

        penalty = 0.18 * len(diff) - 0.04 * len(common)
        return max(0.0, base - penalty)

    @classmethod
    def extract_artist_from_title(cls, raw_title: str, raw_artist: str):
        if cls.looks_like_label(raw_artist) and " - " in raw_title:
            parts = raw_title.split(" - ", 1)
            candidate_artist = parts[0].strip()
            candidate_title  = parts[1].strip()
            if not cls.looks_like_label(candidate_artist):
                return candidate_artist, candidate_title
        return raw_artist, raw_title

    @classmethod
    def enrich_artist_from_title(cls, raw_title: str, raw_artist: str):
        feat_match = re.search(
            r'\b(?:ft\.?|feat\.?|featuring|w/)\s+([^()\[\]]+)',
            raw_title, re.IGNORECASE
        )
        if feat_match:
            feature = feat_match.group(1).strip()
            feature = re.sub(r'\s*[-–—].*$', '', feature).strip()
            if feature.lower() in raw_artist.lower():
                raw_artist = re.sub(re.escape(feature), "", raw_artist, flags=re.IGNORECASE).strip()
            if feature.lower() not in raw_artist.lower():
                raw_artist = raw_artist.strip(" ,&x+")
                raw_artist = f"{raw_artist}, {feature}"
        return raw_title, raw_artist

    @classmethod
    def clean_text(cls, text: str, artist: str = "", field_type: str = "title") -> str:
        if not text:
            return ""

        if field_type == "title":
            cleaned = cls.clean_title(text, artist)
            return cls.normalize(cleaned)
        elif field_type == "artist":
            primary = cls.primary_artist(text)
            return cls.normalize(primary)
        elif field_type == "album":
            cleaned = cls._NOISE.sub("", text)
            cleaned = cleaned.split(" - ")[0].split(" – ")[0]
            cleaned = re.sub(r'\s*[\(\[].*?[\)\]]\s*$', '', cleaned)

            normalized = cls.normalize(cleaned)
            normalized = re.sub(r'\s+', ' ', normalized)
            return normalized.strip()
        elif field_type == "filename":
            return cls.sanitize_filename(text)
        else:
            return cls.normalize(text)

    @classmethod
    def is_collab_album_artist(cls, primary_norm: str, album_artist_norm: str) -> bool:
        """True if album_artist_norm contains primary_norm as a component (collab, not compilation)."""
        if not primary_norm or not album_artist_norm:
            return False
        if primary_norm == album_artist_norm:
            return True
        return bool(re.search(rf'\b{re.escape(primary_norm)}\b', album_artist_norm))