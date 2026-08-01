import re
import logging
from typing import Dict, List
from Algorithm.RegexToken import EditionTokens, FakeAlbumSuffix, CompilationTokens
from Algorithm.TextCleaner import TextCleaner

class ITunesProviderHelper:
    ARTIST_BLACKLIST: Dict[str, List[str]] = {
        "wudo beatz":          ["jackboys", "gang gang"],
        "unleash the archers": ["apex"],
        "jackboy":             ["jackboys 2", "jb2 radio", "2000 excursion"],
    }

    @classmethod
    def is_artist_blacklisted(cls, artist_norm: str, title_norm: str) -> bool:
        return any(
            bad_artist == artist_norm
            and any(bt in title_norm for bt in bad_titles)
            for bad_artist, bad_titles in cls.ARTIST_BLACKLIST.items()
        )

    @staticmethod
    def is_live_item(item: Dict) -> bool:
        return (
            "live" in item.get("trackName", "").lower()
            or "live" in item.get("collectionName", "").lower()
        )

    @staticmethod
    def is_compilation_item(item: Dict, album_sim_pre: float) -> bool:
        if album_sim_pre >= 0.85:
            return False

        if item.get("collectionType") in ("Single", "EP"):
            return False

        is_various = (
            bool(item.get("artistName"))
            and TextCleaner.normalize(item.get("collectionArtistName", "")) == "various artists"
        )
        return (
            (item.get("collectionType") == "Compilation" and is_various)
            or CompilationTokens.match(item.get("collectionName", ""))
        )

    @staticmethod
    def sanitize_hint_album(hint_album: str, title: str, logger: logging.Logger) -> str:
        if not hint_album:
            return ""

        if " - " in hint_album:
            parts  = hint_album.split(" - ", 1)
            first  = parts[0].strip()
            second = parts[1].strip()
            first_has_edition  = EditionTokens.has(first)
            second_is_edition  = bool(second) and EditionTokens.has(second)

            if second and not second_is_edition and len(second) > 4 and not first_has_edition:
                logger.debug(f"[iTunes Helper] hint_album '{hint_album}' → ridotto a '{first}'")
                hint_album = first
            elif first_has_edition and second and not second_is_edition and len(second) > 4:
                logger.debug(f"[iTunes Helper] hint_album '{hint_album}' mantenuto intero (prima parte contiene edition token)")

        norm_hint  = TextCleaner.normalize(hint_album)
        norm_title = TextCleaner.normalize(title)

        if norm_hint == norm_title:
            looks_self_titled = len(norm_hint.replace(" ", "")) <= 20
            if looks_self_titled:
                logger.debug(f"[iTunes Helper] hint_album == title ('{hint_album}') ma sembra self-titled, mantenuto")
            else:
                logger.debug(f"[iTunes Helper] hint_album == title ('{hint_album}'), ignorato")
                return ""

        if FakeAlbumSuffix.has(norm_hint) and FakeAlbumSuffix.strip(norm_hint) == norm_title:
            logger.debug(f"[iTunes Helper] hint_album '{hint_album}' è il titolo + suffisso, ignorato")
            return ""

        sim = TextCleaner.title_similarity(norm_hint, norm_title)
        if sim >= 0.92:
            if EditionTokens.has(norm_hint):
                logger.debug(f"[iTunes Helper] hint_album '{hint_album}' simile al titolo (sim={sim:.2f}) ma contiene edition token, mantenuto")
            else:
                if len(norm_hint.replace(" ", "")) <= 20:
                    logger.debug(f"[iTunes Helper] hint_album '{hint_album}' == title ma corto (len={len(norm_hint)}), mantenuto come self-titled")
                else:
                    return ""

        return hint_album

    @staticmethod
    def has_valid_candidate(
        results: List[Dict], title_norm: str, art_primary: str,
        sim_title_min: float, sim_artist_min: float
    ) -> bool:
        art_norm = TextCleaner.normalize(art_primary) if art_primary else ""
        for item in results:
            if item.get("wrapperType") != "track":
                continue
            clean = TextCleaner.normalize(TextCleaner.clean_title(item.get("trackName", "")))
            if TextCleaner.title_similarity(title_norm, clean) < sim_title_min:
                continue
            if art_norm:
                item_art = TextCleaner.normalize(item.get("artistName", ""))
                if TextCleaner.title_similarity(art_norm, item_art) >= sim_artist_min:
                    return True
            else:
                return True
        return False