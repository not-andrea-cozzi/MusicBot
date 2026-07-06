# MusicBrainzApi.py
import copy
import re
import asyncio
from typing import Optional, List, Dict

import httpx

from Algorithm.TextCleaner import TextCleaner
from Algorithm.BestMatch import TrackMatcher
from Model.Song import Song
from Pipeline.ReleaseEdition import ReleaseEdition


class MusicBrainzApiRequstor:
    """
    Async client for MusicBrainz metadata lookup.
    - Centralised HTTP helper with exponential-backoff retry on 503.
    - Semaphore + 1 req/s rate-limit to avoid burst throttling.
    """

    _BASE_URL: str = "https://musicbrainz.org/ws/2"
    _USER_AGENT: str = "MyMusicApp/1.0.0 (luca.macchi@gmail.com)"
    _TITLE_SUFFIX_RE = re.compile(
        r"\s*[\(\[](bonus track|bonus|deluxe|explicit|clean|radio edit|interlude)[\)\]]",
        re.IGNORECASE,
    )

    # Regex versione per _build_query (fix: preserva remix/version nella query MB)
    _REMIX_RE = re.compile(
        r'[\(\[]([^\)\]]*remix[^\)\]]*|[^\)\]]*version[^\)\]]*|[^\)\]]*edit[^\)\]]*)[\.\]\)]',
        re.IGNORECASE,
    )
    _FEAT_RE = re.compile(
        r'\b(?:ft\.?|feat\.?|featuring|w/)\s+([^(\[\)\]]+)',
        re.IGNORECASE,
    )

    # Estrae il contenuto tra parentesi/brackets (es. "Remix", "Acoustic Live")
    # usato dal fallback con include_version_tag=True per recuperare recording
    # MB che catalogano il tag di versione direttamente nel campo title.
    _PAREN_TAG_RE = re.compile(r'[\(\[]([^\)\]]+)[\)\]]')

    # Versioni "alternative" da scartare quando il titolo originale NON le richiede esplicitamente.
    _ALT_VERSION_RE = re.compile(
        r'\b(instrumental|karaoke|a\s*cappella|acapella|sped\s*up|nightcore|'
        r'slowed(?:\s*(?:and|&)?\s*reverb(?:ed)?)?|8d\s*audio|tiktok\s*remix)\b',
        re.IGNORECASE,
    )

    def __init__(self, client: Optional[httpx.AsyncClient] = None):
        self._matcher = TrackMatcher(min_score=0.5)
        self._client = client or httpx.AsyncClient(
            headers={"User-Agent": self._USER_AGENT}
        )
        self._last_request_time: float = 0.0
        self._semaphore = asyncio.Semaphore(1)

    def set_matcher(self, matcher: TrackMatcher) -> None:
        """Permette al MetadataPipeline di iniettare il matcher condiviso."""
        self._matcher = matcher

    # ── filtri condivisi ──────────────────────────────────────

    @classmethod
    def _is_alt_version(cls, text: str) -> bool:
        return bool(text) and bool(cls._ALT_VERSION_RE.search(text))

    @classmethod
    def _recording_title(cls, recording: Dict) -> str:
        return recording.get("title", "") or ""

    @classmethod
    def _reject_alt_version(cls, recording: Dict, original_title: str) -> bool:
        """
        True se la recording è una versione alternativa (instrumental/karaoke/ecc.)
        che il titolo originale NON richiedeva esplicitamente -> va scartata.
        """
        cand_title = cls._recording_title(recording)
        if cls._is_alt_version(cand_title) and not cls._is_alt_version(original_title):
            return True
        return False

    # ── public API ────────────────────────────────────────────

    async def find_best_recording(self, song: Song) -> Optional[Dict]:
        raw_title = song.meta.title.strip()
        raw_artist = song.meta.artist.strip()
        raw_album = song.meta.album.strip()
        isrc = song.meta.isrc.strip()
        duration_ms = song.meta.duration_ms

        if not raw_title or not raw_artist:
            return None

        clean_title = TextCleaner.normalize(raw_title)
        _rm = self._REMIX_RE.search(raw_title)
        if _rm and _rm.group(1).lower() not in clean_title:
            clean_title = f"{clean_title} ({_rm.group(1).strip().lower()})"
        clean_artist = TextCleaner.normalize(TextCleaner.primary_artist(raw_artist))
        clean_album = TextCleaner.clean_text(raw_album) if raw_album else ""

        if isrc:
            rec = await self.fetch_by_isrc(isrc)
            # Anche con ISRC esatto, verifica che non sia una versione alternativa
            # indesiderata: un ISRC sbagliato a monte non deve propagare
            # silenziosamente un'instrumental/karaoke.
            if rec and not self._reject_alt_version(rec, raw_title):
                return self._clean_recording_dict(rec)
            if rec:
                self._log_skip(f"ISRC {isrc} -> versione alternativa scartata: {self._recording_title(rec)!r}")

        query = self._build_query(raw_title, raw_artist, raw_album)
        candidates = await self._search_recordings(query)
        if not candidates:
            return None

        best_score, best_recording = -1.0, None
        for rec in candidates:
            if self._reject_alt_version(rec, raw_title):
                continue
            score = self._matcher.score_candidate(
                title=clean_title,
                artist=clean_artist,
                album_hint=clean_album or None,
                duration_ms=duration_ms,
                isrc=isrc or None,
                candidate=self._recording_to_candidate(rec),
            )
            if score is not None and score > best_score:
                best_score, best_recording = score, rec

        return self._clean_recording_dict(best_recording) if best_score >= 0.5 else None

    async def fetch_by_isrc(self, isrc: str) -> Optional[Dict]:
        url = f"{self._BASE_URL}/isrc/{isrc}"
        resp = await self._request(url, {"inc": "releases", "fmt": "json"})
        if resp is None:
            return None
        recs = resp.get("recordings", [])
        return recs[0] if recs else None

    async def fetch_recording_by_id(
        self, recording_id: str, inc_params: str = "releases+media"
    ) -> Optional[Dict]:
        url = f"{self._BASE_URL}/recording/{recording_id}"
        return await self._request(url, {"inc": inc_params, "fmt": "json"})

    async def fetch_album_by_id(
        self, release_id: str, inc_params: str = "recordings+artists"
    ) -> Optional[Dict]:
        url = f"{self._BASE_URL}/release/{release_id}"
        return await self._request(url, {"inc": inc_params, "fmt": "json"})

    async def search_album(
        self, album_title: str, artist: str = "", limit: int = 5
    ) -> List[Dict]:
        if not album_title:
            return []

        parts = [self._quote("release", album_title.strip())]
        if artist:
            parts.append(self._quote("artist", artist.strip()))

        data = await self._request(
            f"{self._BASE_URL}/release",
            {"query": " AND ".join(parts), "limit": limit, "fmt": "json"},
        )
        releases = data.get("releases", []) if data else []

        # Fallback: artist credit MB potrebbe differire dall'artista cercato
        if not releases and artist:
            data = await self._request(
                f"{self._BASE_URL}/release",
                {"query": parts[0], "limit": limit, "fmt": "json"},
            )
            releases = data.get("releases", []) if data else []

        return releases

    async def resolve_artist_country(self, artist: str) -> Optional[str]:
        if not artist:
            return None

        data = await self._request(
            f"{self._BASE_URL}/artist/",
            {"query": self._quote("artist", artist), "limit": 5, "fmt": "json", "inc": "area"},
        )
        if not data:
            return None

        artists = data.get("artists", [])
        if not artists:
            return None

        # Cerca il candidato con il nome più simile all'artista cercato.
        norm_target = TextCleaner.normalize(artist)
        best_artist = None
        best_sim    = -1.0
        for cand in artists:
            cand_name = cand.get("name", "")
            sim = TextCleaner.title_similarity(norm_target, TextCleaner.normalize(cand_name))
            if sim > best_sim:
                best_sim, best_artist = sim, cand

        if best_sim < 0.85 or best_artist is None:
            return None

        area = best_artist.get("area")
        if area:
            iso = area.get("iso-3166-1-codes", [])
            return iso[0] if iso else None
        return None

    # alias for pipeline callers using the private-name convention
    async def _resolve_artist_country(self, artist: str) -> Optional[str]:
        return await self.resolve_artist_country(artist)

    async def close(self) -> None:
        await self._client.aclose()

    # ── internal HTTP ─────────────────────────────────────────

    async def _request(
        self,
        url: str,
        params: Dict,
        max_retries: int = 4,
        base_delay: float = 1.0,
    ) -> Optional[Dict]:
        """GET with exponential-backoff retry on 503."""
        for attempt in range(max_retries):
            await self._rate_limit()
            try:
                resp = await self._client.get(url, params=params, timeout=10.0)
                self._last_request_time = asyncio.get_running_loop().time()

                if resp.status_code == 404:
                    return None

                if resp.status_code == 503:
                    wait = base_delay * (2 ** attempt)
                    print(f"[MusicBrainz] 503 – retry {attempt + 1}/{max_retries} in {wait:.1f}s")
                    await asyncio.sleep(wait)
                    continue

                resp.raise_for_status()
                return resp.json()

            except httpx.HTTPStatusError as e:
                print(f"[MusicBrainz] HTTP error {e.response.status_code}: {url}")
                return None
            except Exception as e:
                print(f"[MusicBrainz] Request error: {e} – {url}")
                return None

        print(f"[MusicBrainz] Max retries exceeded: {url}")
        return None

    async def _rate_limit(self) -> None:
        async with self._semaphore:
            now = asyncio.get_running_loop().time()
            elapsed = now - self._last_request_time
            if elapsed < 1.0:
                await asyncio.sleep(1.0 - elapsed)

    async def _search_recordings(self, query: str, limit: int = 10) -> List[Dict]:
        if not query:
            return []
        data = await self._request(
            f"{self._BASE_URL}/recording",
            {"query": query, "limit": limit, "fmt": "json"},
        )
        return data.get("recordings", []) if data else []

    # ── helpers ───────────────────────────────────────────────

    @staticmethod
    def _quote(field: str, value: str) -> str:
        return f'{field}:"{value}"' if " " in value else f"{field}:{value}"

    @staticmethod
    def _log_skip(msg: str) -> None:
        print(f"[MusicBrainz] {msg}")

    def _build_query(
        self,
        title: str,
        artist: str,
        album: str = "",
        include_version_tag: bool = False,
    ) -> str:
        """
        Costruisce la query Lucene per la ricerca recording.

        FIX (refactoring): la versione precedente metteva l'intero titolo,
        incluso un eventuale tag tra parentesi come "(Remix)", in UNA SOLA
        phrase query quotata: `recording:"Brothers (Remix)"`. In Lucene una
        frase tra virgolette richiede un match esatto e consecutivo nel
        campo indicizzato. Se MusicBrainz cataloga la recording semplicemente
        come "Brothers" (caso comune: il tag di versione non è nel campo
        `title` ma in relazioni/disambigua), quella query non trova MAI il
        documento — MB lo scarta prima ancora che un punteggio venga
        calcolato, quindi nessun fix lato TrackMatcher può rimediare a valle.

        Una prima correzione (aggiungere `AND recording:Remix` come termine
        libero) ricade nello STESSO identico problema: l'operatore `AND`
        booleano di Lucene richiede comunque la presenza di entrambi i
        termini nel campo, quindi se "remix" non è indicizzato da nessuna
        parte nel documento, l'AND esclude il documento esattamente come
        prima con la phrase query.

        Fix definitivo: il tag di versione (remix/live/acoustic/ecc.) viene
        OMESSO dalla query MB di default. Recuperiamo il candidato tramite
        il solo titolo "core" (senza tag tra parentesi) + artista, e
        lasciamo che sia `TrackMatcher.score_candidate` (Algorithm/BestMatch.py)
        — che penalizza in modo proporzionale via `remix_mismatch` invece
        di escludere — a decidere se il match è valido. La query deve solo
        massimizzare il recall dei candidati; il giudizio di pertinenza
        spetta allo scoring lato client.

        NUOVO — include_version_tag=True (fallback):
        Se il primo giro (con query "core") restituisce solo recording che
        NON hanno il tag di versione richiesto (es. cerchiamo "Leaked
        (Remix)" ma MB indicizza solo "Leaked" originale), proviamo una
        seconda query che include il tag come PARTE della phrase query
        `recording:"<core> <tag>"`. Questo recupera i casi (meno comuni)
        in cui MB cataloga la recording remix con un title che CONTIENE
        letteralmente il tag (es. "Leaked (Remix)" o "Leaked - Remix"),
        senza reintrodurre il bug originale: se nessuna recording ha quel
        title esatto, la query semplicemente non trova nulla (fallback
        sicuro, non un falso match) e il chiamante (MetadataPipeline)
        scarta comunque ogni risultato il cui titolo non contenga il tag
        (vedi filtro post-query in _resolve_mb).
        """
        clean_title = self._TITLE_SUFFIX_RE.sub("", title).strip()

        # Titolo "core": rimuove qualunque blocco tra parentesi/brackets
        # (remix/live/acoustic/feat/ecc.) per la phrase query esatta.
        core_title = re.sub(r'[\(\[][^\)\]]*[\)\]]', '', clean_title).strip() or clean_title

        parts = []

        if include_version_tag:
            tag_match = self._PAREN_TAG_RE.search(title)
            if tag_match and core_title:
                tag = tag_match.group(1).strip()
                # Rimuove eventuale "feat. ..." dal tag per non sporcare la
                # phrase query con un nome artista (gestito separatamente sotto).
                tag = self._FEAT_RE.sub("", tag).strip(" []()-")
                if tag:
                    parts.append(self._quote("recording", f"{core_title} {tag}"))
                else:
                    parts.append(self._quote("recording", core_title))
            elif core_title:
                parts.append(self._quote("recording", core_title))
        elif core_title:
            parts.append(self._quote("recording", core_title))

        if artist:
            parts.append(self._quote("artist", TextCleaner.primary_artist(artist)))

        feat_in_title = self._FEAT_RE.search(title)
        if feat_in_title:
            feat_artist = feat_in_title.group(1).strip().split(",")[0].strip()
            if feat_artist:
                parts.append(self._quote("artist", feat_artist))

        if album:
            parts.append(self._quote("release", album))
        return " AND ".join(parts)

    def _recording_to_candidate(self, recording: Dict) -> Dict:
        artist_credit = recording.get("artist-credit", [])
        artist_name = "".join(
            ac.get("name", "") + ac.get("joinphrase", "") for ac in artist_credit
        ).strip()

        releases = recording.get("releases", [])
        collection_name = releases[0].get("title", "") if releases else ""

        isrcs = recording.get("isrcs", [])
        return {
            "trackName": TextCleaner.clean_text(recording.get("title", ""), field_type="title"),
            "artistName": TextCleaner.clean_text(artist_name, field_type="artist"),
            "collectionName": TextCleaner.clean_text(collection_name, field_type="album"),
            "trackTimeMillis": recording.get("length"),
            "isrc": isrcs[0] if isrcs else "",
        }

    def _clean_recording_dict(self, rec: Dict) -> Dict:
        cleaned = copy.deepcopy(rec)

        if "title" in cleaned:
            cleaned["title"] = TextCleaner.clean_text(cleaned["title"], field_type="title")

        artist_credit = cleaned.get("artist-credit", [])
        artist_name = "".join(
            ac.get("name", "") + ac.get("joinphrase", "") for ac in artist_credit
        ).strip()
        cleaned["artist_cleaned"] = TextCleaner.clean_text(artist_name, field_type="artist")

        for release in cleaned.get("releases", []):
            if "title" in release:
                release["title"] = TextCleaner.clean_text(release["title"], field_type="album")
            for media in release.get("media", []):
                if media.get("title"):
                    media["title"] = TextCleaner.clean_text(media["title"], field_type="album")
                for track in media.get("tracks", []):
                    if "title" in track:
                        track["title"] = TextCleaner.clean_text(track["title"], field_type="title")

        return cleaned

    # ── ReleaseEdition (NUOVO) ────────────────────────────────

    def edition_for_release(self, release: Dict, title_norm: str = "") -> ReleaseEdition:
        """
        Calcola la ReleaseEdition (single/EP/album) per una release MB già
        risolta (es. quella scelta da MetadataPipeline._pick_best_release).
        Usata per il guard ISRC single-vs-album in VersionGuard.
        """
        return ReleaseEdition.from_mb_release(release, title_norm=title_norm)

    async def get_accurate_artists_by_isrc(self, isrc: str, original_title: str = "") -> tuple[str, str]:
        """
        Risolve gli artisti accurati per un ISRC. Scarta esplicitamente recording
        che risultano essere versioni alternative (instrumental/karaoke/ecc.)
        quando original_title non le richiede.
        """
        if not isrc:
            return "", ""
        url = f"{self._BASE_URL}/recording"
        data = await self._request(url, {"query": f"isrc:{isrc}", "fmt": "json"})
        if not data:
            return "", ""
        recordings = data.get("recordings", [])
        if not recordings:
            return "", ""

        valid = [r for r in recordings if not self._reject_alt_version(r, original_title)]
        if not valid:
            self._log_skip(
                f"ISRC {isrc}: tutte le recording trovate sono versioni alternative, scarto."
            )
            return "", ""

        rec = valid[0]
        credits = rec.get("artist-credit", [])
        artists = "".join(
            c.get("name", "") + c.get("joinphrase", "")
            for c in credits
            if isinstance(c, dict)
        ).strip()
        return artists, rec.get("title", "")