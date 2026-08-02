import re
import unicodedata
from typing import Optional
from Utils.MusicPatterns import MusicPatterns

_ALT_VERSION_RE = MusicPatterns.ALT_VERSION_RE
_VERSION_TAG_RE = MusicPatterns.VERSION_TAG_RE
_DELUXE_RE = MusicPatterns.DELUXE_TAG_RE


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