import re
import unicodedata
from typing import Optional

_ALT_VERSION_RE = re.compile(
    r'\b(instrumental(?:s)?|karaoke|a\s*cappella(?:s)?|acapella(?:s)?|sped\s*up|'
    r'nightcore|slowed(?:\s*(?:and|&)?\s*reverb(?:ed)?)?|8d\s*audio|tiktok\s*remix)\b',
    re.IGNORECASE,
)

_VERSION_TAG_RE = re.compile(
    r'\b(?:remix|re-?mix|radio\s+edit|extended|vip|club\s+mix|'
    r'dub\s+mix|original\s+mix|acoustic|live|demo)\b',
    re.IGNORECASE,
)

_DELUXE_RE = re.compile(
    r'\b(?:deluxe|expanded|super\s+deluxe|anniversary|remastered|'
    r'special\s+edition|bonus\s+track)\b',
    re.IGNORECASE,
)


def is_alt_version(text: str) -> bool:
    return bool(text) and bool(_ALT_VERSION_RE.search(text))


def has_version_tag(text: str) -> bool:
    return bool(text) and bool(_VERSION_TAG_RE.search(text))


def is_deluxe(text: str) -> bool:
    return bool(text) and bool(_DELUXE_RE.search(text))


def clean_query_term(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^\w\s']", " ", s)
    return re.sub(r"\s{2,}", " ", s).strip()


def album_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    ta = set(re.findall(r"\w+", a.lower()))
    tb = set(re.findall(r"\w+", b.lower()))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def best_cover(album_obj: dict) -> Optional[str]:
    for field in ("cover_xl", "cover_big", "cover_medium", "cover"):
        url = album_obj.get(field, "")
        if url:
            return url
    return None


def pick_best_track(tracks: list, hint_album: str) -> Optional[dict]:
    if not tracks:
        return None
    scored = []
    hint_is_deluxe = is_deluxe(hint_album) if hint_album else False
    hint_norm = hint_album.lower().strip() if hint_album else ""

    for t in tracks:
        album = t.get("album", {})
        cover = best_cover(album)
        if not cover:
            continue
        album_title = album.get("title", "")
        album_norm = album_title.lower().strip()
        sim = album_similarity(hint_album, album_title) if hint_album else 0.5
        deluxe = is_deluxe(album_title)

        if hint_norm and album_norm == hint_norm:
            sim = min(1.0, sim + 0.20)
        elif deluxe and not hint_is_deluxe:
            sim -= 0.30
        elif hint_is_deluxe and not deluxe:
            sim -= 0.10

        scored.append((sim, t))

    if not scored:
        return None
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1]


def record_type_to_edition_kind(record_type: str, track_count: int) -> str:
    rt = (record_type or "").lower()
    if rt == "single":
        return "single"
    if rt == "ep":
        return "ep"
    if rt == "album":
        return "album"
    if rt == "compile":
        return "compilation"
    if track_count and track_count <= 2:
        return "single"
    return "unknown"