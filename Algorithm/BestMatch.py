from __future__ import annotations

import re
from functools import total_ordering, lru_cache
from typing import Optional
from unidecode import unidecode
from rapidfuzz.distance import Indel, JaroWinkler

from Algorithm.RegexToken import EditionTokens

_VA_ARTISTS: frozenset[str] = frozenset(
    ("", "various artists", "various", "va", "unknown", "various artists & various")
)

_SD_END_WORDS: tuple[str, ...] = ("the", "a", "an")

_SD_PATTERNS: tuple[tuple[str, float], ...] = (
    (r"^the\s+",                             0.1),
    (r"[\[\(]?\s*(ep|single)\s*[\]\)]?",     0.0),
    (r"[\[\(]?\s*(featuring|feat\.?|ft\.?)\s*.+", 0.1),
    (r"\(.*?\)",                             0.3),
    (r"\[.*?\]",                             0.3),
)

_SD_REPLACE: tuple[tuple[str, str], ...] = (
    (r"\s*&\s*", " and "),
    (r"\s*'\s*",  "'"),
)

_PREFER_SINGLE: bool = False

_DEFAULT_WEIGHTS: dict[str, float] = {
    "track_title":       6.0,
    "track_artist":      3.0,
    "track_length":      2.0,
    "album":             2.0,
    "album_edition":     2.0,
    "collection_type":   0.5,
    "single_bonus":      3.5 if _PREFER_SINGLE else 0.5,
    "track_id":         10.0,
    "live_mismatch":     3.0,
    "remix_mismatch":    3.0,
    "compilation_penalty": 2.0,
}

_TITLE_HARD_REJECT: float = 0.35
_TITLE_ARTIST_DEWEIGHT_THRESHOLD: float = 0.08
_ARTIST_HARD_REJECT: float = 0.80

_REMIX_RE = re.compile(
    r"\b(?:remix(?:ed)?|radio\s+edit|extended\s+mix|club\s+mix|"
    r"dub\s+mix|original\s+mix|vip\s+mix)\b",
    re.IGNORECASE,
)

_LIVE_RE = re.compile(r"\blive\b", re.IGNORECASE)

_SINGLE_EP_SUFFIX_RE = re.compile(r"\s*-\s*(single|ep)\s*$", re.IGNORECASE)

_ALNUM_RE = re.compile(r"[^a-z0-9]")

_SD_COMPILED: tuple[tuple[re.Pattern, float], ...] = tuple(
    (re.compile(pat, re.IGNORECASE), w) for pat, w in _SD_PATTERNS
)
_SD_REPLACE_COMPILED: tuple[tuple[re.Pattern, str], ...] = tuple(
    (re.compile(pat), repl) for pat, repl in _SD_REPLACE
)


@total_ordering
class Distance:
    __slots__ = ("_weights", "_penalties")

    def __init__(self, weights: dict[str, float] | None = None) -> None:
        self._weights: dict[str, float] = weights if weights is not None else _DEFAULT_WEIGHTS
        self._penalties: dict[str, list[float]] = {}

    @property
    def distance(self) -> float:
        dmax = self.max_distance
        return self.raw_distance / dmax if dmax else 0.0

    @property
    def max_distance(self) -> float:
        return sum(
            len(v) * self._weights.get(k, 1.0)
            for k, v in self._penalties.items()
        )

    @property
    def raw_distance(self) -> float:
        return sum(
            sum(v) * self._weights.get(k, 1.0)
            for k, v in self._penalties.items()
        )

    def __float__(self) -> float:
        return self.distance

    def __bool__(self) -> bool:
        return self.distance > 0.0

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Distance):
            return self.distance == other.distance
        if isinstance(other, float):
            return self.distance == other
        return NotImplemented

    def __lt__(self, other: object) -> bool:
        if isinstance(other, Distance):
            return self.distance < other.distance
        if isinstance(other, float):
            return self.distance < other
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.distance)

    def __str__(self) -> str:
        return f"{self.distance:.4f}"

    def __repr__(self) -> str:
        parts = ", ".join(
            f"{k}={sum(v):.3f}*{self._weights.get(k,1.0)}"
            for k, v in self._penalties.items()
            if v
        )
        return f"Distance({self.distance:.4f} [{parts}])"

    def __getitem__(self, key: str) -> float:
        raw = sum(self._penalties.get(key, [])) * self._weights.get(key, 1.0)
        dmax = self.max_distance
        return raw / dmax if dmax else 0.0

    def add(self, key: str, dist: float) -> None:
        self._penalties.setdefault(key, []).append(max(0.0, min(1.0, float(dist))))

    def add_string(self, key: str, s1: Optional[str], s2: Optional[str]) -> None:
        self.add(key, string_dist(s1, s2))

    def add_expr(self, key: str, expr: bool) -> None:
        self.add(key, 1.0 if expr else 0.0)

    def add_ratio(self, key: str, numerator: float, denominator: float) -> None:
        if denominator:
            self.add(key, numerator / denominator)
        else:
            self.add(key, 0.0)

    def add_number(self, key: str, n1: int, n2: int) -> None:
        diff = abs(n1 - n2)
        for _ in range(diff or 1):
            self.add(key, 1.0 if diff else 0.0)

    def update(self, other: Distance) -> None:
        for k, vals in other._penalties.items():
            self._penalties.setdefault(k, []).extend(vals)

    def items(self) -> list[tuple[str, float]]:
        result = [(k, self[k]) for k in self._penalties if self._penalties[k]]
        return sorted(result, key=lambda kv: -kv[1])


@lru_cache(maxsize=4096)
def _alnum(s: str) -> str:
    s = re.sub(r'[®™©]', '', s)
    return _ALNUM_RE.sub("", unidecode(s).lower())


@lru_cache(maxsize=4096)
def _string_dist_basic(s1: str, s2: str) -> float:
    a, b = _alnum(s1), _alnum(s2)
    if not a and not b:
        return 0.0
    if not a or not b:
        return 1.0
    if a == b:
        return 0.0
    if min(len(a), len(b)) <= 6:
        return 1.0 - JaroWinkler.normalized_similarity(a, b)
    return Indel.normalized_distance(a, b)


@lru_cache(maxsize=4096)
def string_dist(s1: Optional[str], s2: Optional[str]) -> float:
    if s1 is None and s2 is None:
        return 0.0
    if s1 is None or s2 is None:
        return 1.0

    w1 = s1.lower().strip()
    w2 = s2.lower().strip()

    if w1 == w2:
        return 0.0

    for art in _SD_END_WORDS:
        suffix = f", {art}"
        if w1.endswith(suffix):
            w1 = f"{art} {w1[: -len(suffix)]}"
        if w2.endswith(suffix):
            w2 = f"{art} {w2[: -len(suffix)]}"

    for pat, repl in _SD_REPLACE_COMPILED:
        w1 = pat.sub(repl, w1).strip()
        w2 = pat.sub(repl, w2).strip()

    base = _string_dist_basic(w1, w2)
    penalty = 0.0

    for pat, weight in _SD_COMPILED:
        c1 = pat.sub("", w1).strip() if pat.search(w1) else w1
        c2 = pat.sub("", w2).strip() if pat.search(w2) else w2

        if c1 == w1 and c2 == w2:
            continue

        new_dist = _string_dist_basic(c1, c2)
        delta = max(0.0, base - new_dist)
        if delta == 0.0:
            continue

        w1, w2 = c1, c2
        base = new_dist
        penalty += weight * delta

    return min(1.0, base + penalty)


def similarity(s1: Optional[str], s2: Optional[str]) -> float:
    return 1.0 - string_dist(s1, s2)


def _edition_tokens(s: str) -> frozenset[str]:
    return frozenset(m.lower() for m in EditionTokens.findall(s))


def _is_single_or_ep(candidate: dict) -> bool:
    ct = candidate.get("collectionType", "")
    album = (candidate.get("collectionName", "") or "").strip()
    return ct in ("Single", "EP") or bool(_SINGLE_EP_SUFFIX_RE.search(album))


def _is_compilation(candidate: dict) -> bool:
    return candidate.get("collectionType", "") == "Compilation"


def _primary_artist_contained(primary: str, candidate_artist: str) -> bool:
    if not primary or not candidate_artist:
        return False
    a_prim = _alnum(primary)
    a_cand = _alnum(candidate_artist)
    return bool(a_prim) and a_prim in a_cand


def strip_parenthetical(s: str) -> str:
    return re.sub(r'[\(\[][^\)\]]*[\)\]]', '', s).strip()


_strip_parenthetical = strip_parenthetical


def _title_is_truncation(title_norm: str, cand_title: str) -> float:
    a = _alnum(_strip_parenthetical(title_norm))
    b = _alnum(_strip_parenthetical(cand_title))
    if not a or not b or len(b) >= len(a):
        return 0.0
    if not a.startswith(b):
        return 0.0
    return 1.0 - len(b) / len(a)


def _strip_single_ep(text: str) -> str:
    return _SINGLE_EP_SUFFIX_RE.sub("", text).strip()


def _has_live(text: str) -> bool:
    return bool(_LIVE_RE.search(text))


def _has_remix(text: str) -> bool:
    return bool(_REMIX_RE.search(text))


def _is_eponymous_single(title: str, cand_album: str) -> bool:
    if not _is_single_or_ep_name(cand_album):
        return False
    stripped = _strip_single_ep(cand_album)
    return string_dist(title, stripped) < 0.15


def _is_single_or_ep_name(album: str) -> bool:
    return bool(_SINGLE_EP_SUFFIX_RE.search(album))


class TrackMatcher:
    def __init__(
        self,
        weights: dict[str, float] | None = None,
        title_hard_reject: float = _TITLE_HARD_REJECT,
        min_score: float = 0.5,
    ) -> None:
        self._weights = weights if weights is not None else dict(_DEFAULT_WEIGHTS)
        self._title_hard_reject = title_hard_reject
        self._min_score = min_score

    def score_candidate(
        self, *, title: str, artist: str = "", album_hint: str = "",
        duration_ms: Optional[int] = None, isrc: str = "", candidate: dict,
    ) -> Optional[float]:
        dist = self._compute_distance(
            title=title, artist=artist, album_hint=album_hint,
            duration_ms=duration_ms, isrc=isrc, candidate=candidate,
        )
        if dist is None:
            return None
        score = 1.0 - float(dist)
        return score if score >= self._min_score else None

    def compute_distance(
        self, *, title: str, artist: str = "", album_hint: str = "",
        duration_ms: Optional[int] = None, isrc: str = "", candidate: dict,
    ) -> Optional[Distance]:
        return self._compute_distance(
            title=title, artist=artist, album_hint=album_hint,
            duration_ms=duration_ms, isrc=isrc, candidate=candidate,
        )

    def _compute_distance(
        self, *, title: str, artist: str, album_hint: str,
        duration_ms: Optional[int], isrc: str, candidate: dict,
    ) -> Optional[Distance]:

        cand_title  = (candidate.get("trackName",      "") or "").strip()
        cand_artist = (candidate.get("artistName",     "") or "").strip()
        cand_album  = (candidate.get("collectionName", "") or "").strip()
        cand_ms     = candidate.get("trackTimeMillis")
        cand_isrc   = (candidate.get("isrc",           "") or "").strip()

        dist = Distance(self._weights)

        isrc_match = bool(isrc) and bool(cand_isrc) and isrc.upper() == cand_isrc.upper()
        if isrc_match:
            dist.add("track_id", 0.0)
            dist.add("track_title",  string_dist(title, cand_title)  * 0.1)
            dist.add("track_artist", string_dist(artist, cand_artist) * 0.1)
            return dist

        title_dist = string_dist(title, cand_title)
        truncation_penalty = _title_is_truncation(title, cand_title)
        if truncation_penalty > 0.25:
            return None
        effective_title_dist = min(1.0, title_dist + truncation_penalty * 0.3)
        if effective_title_dist > self._title_hard_reject:
            return None

        dist.add("track_title", effective_title_dist)

        src_live  = _has_live(title)  or _has_live(album_hint)
        cand_live = _has_live(cand_title) or _has_live(cand_album)
        if src_live != cand_live:
            dist.add("live_mismatch", 1.0)

        src_remix  = _has_remix(title)  or _has_remix(album_hint)
        cand_remix = _has_remix(cand_title) or _has_remix(cand_album)
        if src_remix != cand_remix:
            dist.add("remix_mismatch", 1.0)

        if artist and cand_artist.lower() not in _VA_ARTISTS:
            artist_dist = string_dist(artist, cand_artist)

            if _primary_artist_contained(artist, cand_artist):
                artist_dist = 0.0

            if artist_dist > _ARTIST_HARD_REJECT:
                return None

            if effective_title_dist < _TITLE_ARTIST_DEWEIGHT_THRESHOLD:
                artist_dist *= 0.5

            dist.add("track_artist", artist_dist)

        if duration_ms and cand_ms:
            grace    = 2_000
            max_diff = 15_000
            delta = abs(cand_ms - duration_ms) - grace
            dist.add_ratio("track_length", delta, max_diff)

        is_single_ep = _is_single_or_ep(candidate)
        eponymous    = _is_eponymous_single(title, cand_album)
        src_is_album = bool(album_hint) and not _is_single_or_ep_name(album_hint)

        if eponymous and not src_is_album:
            dist.add("single_bonus", 0.0)
            dist.add("collection_type", 0.0)
        elif is_single_ep and not src_is_album:
            dist.add("single_bonus", 0.3)
            dist.add("collection_type", 0.0)
        elif src_is_album and not is_single_ep:
            dist.add("single_bonus", 0.0)
            dist.add("collection_type", 0.0)
        else:
            dist.add("single_bonus", 1.0)
            dist.add("collection_type", 0.5)

        if album_hint:
            dist.add_string("album", album_hint, cand_album)

            hint_ed = _edition_tokens(album_hint)
            cand_ed = _edition_tokens(cand_album)

            if hint_ed or cand_ed:
                diff_count   = len(hint_ed.symmetric_difference(cand_ed))
                common_count = len(hint_ed & cand_ed)
                union_count  = max(len(hint_ed | cand_ed), 1)
                edition_dist = max(0.0, min(1.0,
                    (diff_count - 0.3 * common_count) / union_count
                ))

                if hint_ed != cand_ed:
                    dist.add("album_edition", edition_dist * 2.0)
                else:
                    dist.add("album_edition", edition_dist)
        else:
            cand_ed = _edition_tokens(cand_album)
            if cand_ed:
                dist.add("album_edition", 2.0)

        if _is_compilation(candidate):
            dist.add("compilation_penalty", 1.0)

        return dist


_default_matcher = TrackMatcher()


def score_candidate(
    *, title: str, artist: str = "", album_hint: str = "",
    duration_ms: Optional[int] = None, isrc: str = "", candidate: dict,
    weights: dict[str, float] | None = None, min_score: float = 0.5,
) -> Optional[float]:

    if weights is not None or min_score != 0.5:
        matcher = TrackMatcher(weights=weights, min_score=min_score)
    else:
        matcher = _default_matcher

    return matcher.score_candidate(
        title=title, artist=artist, album_hint=album_hint,
        duration_ms=duration_ms, isrc=isrc, candidate=candidate,
    )