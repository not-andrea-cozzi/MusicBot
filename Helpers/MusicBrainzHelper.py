# MusicBrainzHelper.py
import re
import logging
from typing import Optional
from Model.Song import Song
from Algorithm.TextCleaner import TextCleaner


class MusicBrainzHelper:
    """
    Gestisce l'estrazione e l'applicazione dei metadati dalle risposte
    complesse dell'API di MusicBrainz tramite metodi statici.
    """

    _VINYL_SIDE_MAP = {"A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "F": 6}
    _VINYL_RE = re.compile(r'^([A-F])(\d+)$', re.IGNORECASE)
    _MB_FAKE_COUNTRY_CODES = {"XW", "XE", "XU", "XX"}
    _VALID = frozenset({
        "US","GB","CA","AU","NZ","IE","FR","DE","IT","ES","PT","NL",
        "BE","CH","AT","SE","NO","DK","FI","PL","RU","GR","TR","JP",
        "KR","CN","TW","HK","IN","SG","MY","ID","TH","PH","ZA","MX",
        "BR","AR","CL","CO","AE","SA","IL",
    })

    # Stesso pattern usato in MusicBrainzApi/SpotifyProvider: scarta versioni
    # alternative (instrumental/karaoke/ecc.) quando il titolo originale non
    # le richiede esplicitamente.
    _ALT_VERSION_RE = re.compile(
        r'\b(instrumental|karaoke|a\s*cappella|acapella|sped\s*up|nightcore|'
        r'slowed(?:\s*(?:and|&)?\s*reverb(?:ed)?)?|8d\s*audio|tiktok\s*remix)\b',
        re.IGNORECASE,
    )

    @classmethod
    def _is_alt_version(cls, text: str) -> bool:
        return bool(text) and bool(cls._ALT_VERSION_RE.search(text))

    @classmethod
    def _track_is_rejectable(cls, track_title: str, song_title: str) -> bool:
        """True se track_title è una versione alternativa non richiesta da song_title."""
        return cls._is_alt_version(track_title) and not cls._is_alt_version(song_title)

    @staticmethod
    def parse_track_number(raw: str, all_tracks: list) -> Optional[int]:
        raw = raw.strip().upper()
        if raw.isdigit():
            return int(raw)

        m = MusicBrainzHelper._VINYL_RE.match(raw)
        if m:
            current_side = m.group(1)
            track_on_side = int(m.group(2))
            offset = sum(
                1 for t in all_tracks
                if MusicBrainzHelper._VINYL_RE.match(t.get("number", "").upper())
                and t.get("number", "").upper()[0] < current_side
            )
            return offset + track_on_side
        return None

    @staticmethod
    def _resolve_media_and_track(
        mb_detail: dict,
        song_title: str,
        song_album: str,
        log: logging.Logger,
    ) -> tuple[dict, dict, list]:
        """
        Risolve first_release, first_media, first_track da mb_detail
        gestendo due strutture diverse:

        Struttura A — mb_detail è una RECORDING (da find_best_recording):
            mb_detail["releases"][0]["media"][0]["tracks"][0]

        Struttura B — mb_detail è un RELEASE/ALBUM (da fetch_album_by_id):
            mb_detail["media"][0]["tracks"][0]
            (non ha "releases" al top level — è già il release)

        La selezione della traccia scarta esplicitamente versioni alternative
        (instrumental/karaoke/ecc.) quando song_title non le richiede, per non
        propagare metadati (numero traccia, disco, ecc.) presi dalla versione
        sbagliata di un brano con più edizioni nello stesso album.

        Restituisce (first_media, first_track, all_tracks_in_media).
        """
        releases = mb_detail.get("releases", [])
        is_release_direct = (
            not releases and "media" in mb_detail
        ) or (
            "media" in mb_detail and mb_detail.get("media")
        )

        if is_release_direct:
            media_list = mb_detail.get("media", [])
            log.debug(f"[MB-Helper] mb_detail è un release diretto, media count={len(media_list)}")
        else:
            best_release = None
            if song_album:
                for r in releases:
                    r_title = TextCleaner.clean_text(r.get("title", ""), field_type="album")
                    if r_title and (song_album in r_title or r_title in song_album):
                        best_release = r
                        break
            first_release = best_release or (releases[0] if releases else {})
            media_list = first_release.get("media", [])
            log.debug(f"[MB-Helper] mb_detail è una recording, media count={len(media_list)}")

        if not media_list:
            log.debug("[MB-Helper] Nessun media trovato in mb_detail")
            return {}, {}, []

        first_media: dict = {}
        first_track: dict = {}
        fallback_media: dict = {}
        fallback_track: dict = {}

        for media in media_list:
            for t in media.get("tracks", []):
                t_title = TextCleaner.clean_text(t.get("title", ""), field_type="title")
                if not (t_title and song_title and (
                    song_title in t_title or t_title in song_title
                )):
                    continue

                if MusicBrainzHelper._track_is_rejectable(t.get("title", ""), song_title):
                    # Tieni una riserva nel caso non si trovi nulla di meglio,
                    # ma preferisci sempre una traccia non-alternativa.
                    if not fallback_track:
                        fallback_media, fallback_track = media, t
                    log.debug(f"[MB-Helper] Traccia scartata (versione alternativa): {t_title!r}")
                    continue

                first_media, first_track = media, t
                break
            if first_track:
                break

        # Nessuna traccia "pulita" trovata: usa la riserva alternativa solo come
        # ultima spiaggia, meglio di niente ma mai preferita a una valida.
        if not first_track and fallback_track:
            first_media, first_track = fallback_media, fallback_track
            log.debug("[MB-Helper] Nessuna traccia valida, riuso versione alternativa come fallback")

        if not first_media and media_list:
            first_media = media_list[0]
        if not first_track:
            track_list = first_media.get("tracks", []) if isinstance(first_media, dict) else []
            first_track = track_list[0] if track_list else {}

        all_tracks = first_media.get("tracks", []) if isinstance(first_media, dict) else []
        return first_media, first_track, all_tracks

    @staticmethod
    def apply_fallback_with_reference(
        song: Song,
        mb_detail: dict,
        mb_reference: dict,
        logger: Optional[logging.Logger] = None
    ) -> None:
        log = logger or logging.getLogger(__name__)

        ref_title = mb_reference.get("title", "") if mb_reference else ""
        ref_album = ""
        if mb_reference and mb_reference.get("releases"):
            ref_album = mb_reference["releases"][0].get("title", "")

        song_album = TextCleaner.clean_text(
            ref_album or song.meta.album, field_type="album"
        )
        song_title = TextCleaner.clean_text(
            ref_title or song.meta.title, field_type="title"
        )

        first_media, first_track, all_tracks = MusicBrainzHelper._resolve_media_and_track(
            mb_detail, song_title, song_album, log
        )

        # ── Genre ────────────────────────────────────────────────────────────
        if not song.meta.genre:
            genre_tag = ""

            def extract_genre(entity: dict) -> str:
                genres = entity.get("genres", [])
                if genres:
                    best = max(genres, key=lambda x: x.get("count", 0), default=genres[0])
                    return best.get("name", "")

                tags = entity.get("tags", [])
                if tags:
                    best = max(tags, key=lambda x: x.get("count", 0), default=tags[0])
                    return best.get("name", "")
                return ""

            if mb_reference:
                genre_tag = extract_genre(mb_reference)

                if not genre_tag and mb_reference.get("release-groups"):
                    genre_tag = extract_genre(mb_reference["release-groups"][0])

            if not genre_tag and mb_detail:
                if mb_detail.get("release-group"):
                    genre_tag = extract_genre(mb_detail["release-group"])

                if not genre_tag:
                    genre_tag = extract_genre(mb_detail)

            if genre_tag:
                song.meta.genre = genre_tag.title()
                log.debug(f"[MB-Helper] genre = {genre_tag.title()!r}")

        # ── Track number ─────────────────────────────────────────────────────
        if not song.meta.track_number:
            raw_number = first_track.get("number", "")
            if raw_number:
                track_int = MusicBrainzHelper.parse_track_number(raw_number, all_tracks)
                if track_int:
                    song.meta.track_number = track_int
                    log.debug(f"[MB-Helper] track_number = {track_int}")
            else:
                log.debug("[MB-Helper] track number non trovato in first_track")

        # ── Disc number ──────────────────────────────────────────────────────
        if not song.meta.disc_number:
            dn = first_media.get("position")
            if dn:
                try:
                    song.meta.disc_number = int(dn)
                    log.debug(f"[MB-Helper] disc_number = {dn}")
                except (ValueError, TypeError):
                    pass
            else:
                log.debug("[MB-Helper] disc_number non trovato in first_media")

        # ── Year ─────────────────────────────────────────────────────────────
        if not song.meta.year:
            date_str = ""
            if "date" in mb_detail:
                date_str = mb_detail.get("date", "")
            elif mb_reference:
                date_str = mb_reference.get("first-release-date", "")
            if date_str and len(date_str) >= 4:
                song.meta.year = date_str[:4]
                log.debug(f"[MB-Helper] year = {date_str[:4]!r}")

        # ── Album ────────────────────────────────────────────────────────────
        if not song.meta.album:
            album_title = mb_detail.get("title", "")
            if album_title:
                song.meta.album = album_title
                log.debug(f"[MB-Helper] album = {album_title!r}")

    @staticmethod
    def apply_exclusive(song: Song, mb_result: dict, logger: Optional[logging.Logger] = None) -> None:
        log = logger or logging.getLogger(__name__)
        releases = mb_result.get("releases", [])

        song_album = TextCleaner.clean_text(song.meta.album, field_type="album")
        best_release = None
        if song_album:
            for r in releases:
                r_title = TextCleaner.clean_text(r.get("title", ""), field_type="album")
                if r_title and (song_album in r_title or r_title in song_album):
                    best_release = r
                    break
        first_release = best_release or (releases[0] if releases else {})

        release_group = first_release.get("release-group", {})
        artist_credits = mb_result.get("artist-credit", [])

        if not song.meta.isrc:
            isrcs = mb_result.get("isrcs", [])
            if isrcs:
                song.meta.isrc = isrcs[0]
                log.debug(f"[MB-Helper] isrc = {isrcs[0]!r}")

        if not song.meta.mb_track_id:
            mb_id = mb_result.get("id", "")
            if mb_id:
                song.meta.mb_track_id = mb_id
                log.debug(f"[MB-Helper] mb_track_id = {mb_id!r}")

        if not song.meta.mb_album_id:
            rel_id = first_release.get("id", "")
            if rel_id:
                song.meta.mb_album_id = rel_id
                log.debug(f"[MB-Helper] mb_album_id = {rel_id!r}")

        if not song.meta.mb_release_group_id:
            rg_id = release_group.get("id", "")
            if rg_id:
                song.meta.mb_release_group_id = rg_id
                log.debug(f"[MB-Helper] mb_release_group_id = {rg_id!r}")

        if not song.meta.mb_artist_id:
            for ac in artist_credits:
                if isinstance(ac, dict):
                    art_id = ac.get("artist", {}).get("id", "")
                    if art_id:
                        song.meta.mb_artist_id = art_id
                        log.debug(f"[MB-Helper] mb_artist_id = {art_id!r}")
                        break

        if not song.meta.mb_album_artist_id:
            for ac in first_release.get("artist-credit", []):
                if isinstance(ac, dict):
                    art_id = ac.get("artist", {}).get("id", "")
                    if art_id:
                        song.meta.mb_album_artist_id = art_id
                        log.debug(f"[MB-Helper] mb_album_artist_id = {art_id!r}")
                        break

        if not song.meta.label:
            label_info_list = first_release.get("label-info", [])
            if label_info_list:
                label_name = label_info_list[0].get("label", {}).get("name", "")
                if label_name:
                    song.meta.label = label_name
                    log.debug(f"[MB-Helper] label = {label_name!r}")

        if not song.meta.country:
            country = (first_release.get("country") or "").strip().upper()
            if country and len(country) == 2 and country not in MusicBrainzHelper._MB_FAKE_COUNTRY_CODES and country in MusicBrainzHelper._VALID:
                song.meta.country = country
                log.debug(f"[MB-Helper] country = {country!r}")
            elif country:
                log.debug(f"[MB-Helper] country '{country}' scartato")