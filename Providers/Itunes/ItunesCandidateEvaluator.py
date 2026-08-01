from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from Algorithm.RegexToken import EditionTokens
from Algorithm.BestMatch import score_candidate, strip_parenthetical as _strip_parenthetical
from Algorithm.TextCleaner import TextCleaner
from Pipeline.ReleaseEdition import ReleaseEdition
from Providers.Itunes.ItunesHelper import ITunesProviderHelper


@dataclass
class _SearchCandidate:
    """Coppia (score, item) con edizione calcolata una sola volta."""
    score: float
    item: Dict[str, Any]
    edition: ReleaseEdition


class ITunesCandidateEvaluator:
    """
    Logica pura di scoring/filtering/selezione dei candidati iTunes.
    Nessun I/O: riceve liste di risultati grezzi e restituisce il best pick.
    """

    SIM_TITLE_MIN = 0.75
    SIM_ARTIST_MIN = 0.85
    SIM_ALBUM_MIN = 0.85
    HIGH_CONFIDENCE = 0.90

    def __init__(self, prefer_album: bool = False, prefer_explicit: bool = True) -> None:
        self.prefer_album = prefer_album
        self.prefer_explicit = prefer_explicit

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------
    def evaluate(
        self,
        results: List[Dict],
        *,
        title: str,
        title_norm: str,
        art_primary: str,
        hint_album_norm: str = "",
        duration_ms: Optional[int] = None,
        min_score: float = 0.5,
    ) -> Tuple[Optional[Dict], float, Optional[ReleaseEdition]]:
        norm_art = TextCleaner.normalize(art_primary) if art_primary else ""
        candidates: List[_SearchCandidate] = []

        for item in results:
            if item.get("wrapperType") != "track" or item.get("kind") not in ("song", "music-video"):
                continue
            if not item.get("trackName"):
                continue
            if not self._passes_prefilters(item, title, title_norm, hint_album_norm):
                continue

            score = score_candidate(
                title=title_norm, artist=norm_art, album_hint=hint_album_norm,
                duration_ms=duration_ms, isrc="", candidate=item, min_score=min_score,
            )
            if score is None:
                continue
            if not self._passes_final_filters(item, score, title, title_norm, art_primary, hint_album_norm):
                continue

            edition = ReleaseEdition.from_collection(
                collection_type=item.get("collectionType", ""),
                collection_name=item.get("collectionName", ""),
                track_count=item.get("trackCount") or 0,
                title_norm=title_norm,
            )
            candidates.append(_SearchCandidate(score=score, item=item, edition=edition))

        best = self._pick_best(candidates, duration_ms=duration_ms)
        if best is None:
            return None, -1.0, None

        best = self._maybe_swap_album_single(best, candidates, title_norm, norm_art)
        return best.item, best.score, best.edition

    # ------------------------------------------------------------------
    # Filtering
    # ------------------------------------------------------------------
    def _passes_prefilters(self, item: Dict, title: str, title_norm: str, hint_album_norm: str) -> bool:
        """Filtri di esclusione rapidi, prima dello scoring costoso."""
        if ITunesProviderHelper.is_artist_blacklisted(
            TextCleaner.normalize(item.get("artistName", "")), title_norm
        ):
            return False

        album_sim_pre = (
            TextCleaner.album_edition_similarity(
                hint_album_norm, TextCleaner.normalize(item.get("collectionName", ""))
            ) if hint_album_norm else 0.0
        )
        if ITunesProviderHelper.is_compilation_item(item, album_sim_pre):
            return False

        if re.search(r'[\(\[]\s*mixed\s*[\)\]]', item.get("trackName", ""), re.IGNORECASE):
            return False

        # Confronto sul titolo "core" (senza tag tra parentesi/brackets come
        # remix/live/acoustic): un tag di versione non deve impedire il
        # match qui — ci pensa già `remix_mismatch`/`live_mismatch` dentro
        # score_candidate, in modo proporzionale invece che con un reject
        # secco.
        title_clean_norm = TextCleaner.normalize(_strip_parenthetical(TextCleaner.clean_title(title)))
        cand_clean_norm  = TextCleaner.normalize(_strip_parenthetical(TextCleaner.clean_title(item.get("trackName", ""))))
        if TextCleaner.title_similarity(title_clean_norm, cand_clean_norm) < 0.88:
            return False

        return True

    def _passes_final_filters(
        self, item: Dict, score: float, title: str, title_norm: str,
        art_primary: str, hint_album_norm: str,
    ) -> bool:
        core_title_norm = TextCleaner.normalize(_strip_parenthetical(title_norm))
        cand_title_norm = TextCleaner.normalize(_strip_parenthetical(TextCleaner.clean_title(item.get("trackName", ""))))
        title_sim = TextCleaner.title_similarity(core_title_norm, cand_title_norm)
        if title_sim < self.SIM_TITLE_MIN and cand_title_norm != core_title_norm:
            return False

        if art_primary:
            norm_art      = TextCleaner.normalize(art_primary)
            norm_cand_art = TextCleaner.normalize(item.get("artistName", ""))
            norm_coll_art = TextCleaner.normalize(item.get("collectionArtistName", ""))
            artist_sim    = TextCleaner.title_similarity(norm_art, norm_cand_art)
            coll_sim      = TextCleaner.title_similarity(norm_art, norm_coll_art) if norm_coll_art else 0.0
            substring_match = (
                re.search(rf'\b{re.escape(norm_art)}\b', norm_cand_art)
                or (norm_coll_art and (norm_art in norm_coll_art or norm_coll_art in norm_art))
            )
            if artist_sim < self.SIM_ARTIST_MIN and coll_sim < self.SIM_ARTIST_MIN and not substring_match:
                return False

        if score >= self.HIGH_CONFIDENCE:
            return True

        if hint_album_norm:
            cand_album_norm = TextCleaner.normalize(item.get("collectionName", ""))
            album_sim = TextCleaner.album_edition_similarity(hint_album_norm, cand_album_norm)
            hint_stripped = re.sub(r'[^a-z0-9\s]', '', hint_album_norm).strip()
            cand_stripped = re.sub(r'[^a-z0-9\s]', '', cand_album_norm).strip()
            hint_is_prefix = bool(cand_stripped) and hint_stripped.startswith(cand_stripped)
            artist_exact = (
                art_primary
                and TextCleaner.title_similarity(
                    TextCleaner.normalize(art_primary), TextCleaner.normalize(item.get("artistName", ""))
                ) >= self.SIM_ARTIST_MIN
            )
            if album_sim < self.SIM_ALBUM_MIN and score < 0.95 and not hint_is_prefix and not artist_exact:
                return False

        return True

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------
    def _pick_best(
        self, candidates: List[_SearchCandidate], duration_ms: Optional[int] = None,
    ) -> Optional[_SearchCandidate]:
        if not candidates:
            return None

        def _is_preferred_explicitness(c: _SearchCandidate) -> bool:
            is_explicit = str(c.item.get("trackExplicitness", "")).lower() == "explicit"
            return is_explicit if self.prefer_explicit else not is_explicit

        def _duration_delta(c: _SearchCandidate) -> int:
            if duration_ms is None:
                return 0
            cand_ms = c.item.get("trackTimeMillis")
            return abs(int(duration_ms) - int(cand_ms)) if cand_ms else 999_999

        preferred: Dict[int, _SearchCandidate] = {}
        fallback: Dict[int, _SearchCandidate] = {}
        orphans: List[_SearchCandidate] = []

        for c in candidates:
            cid = c.item.get("collectionId")
            bucket = preferred if _is_preferred_explicitness(c) else fallback
            if cid:
                if cid not in bucket or c.score > bucket[cid].score:
                    bucket[cid] = c
            else:
                orphans.append(c)

        pool = list(preferred.values()) + [c for cid, c in fallback.items() if cid not in preferred] + orphans

        def _sort_key(c: _SearchCandidate) -> Tuple[float, bool, int]:
            return (c.score, not ITunesProviderHelper.is_live_item(c.item), -_duration_delta(c))

        return max(pool, key=_sort_key)

    def _maybe_swap_album_single(
        self,
        best: _SearchCandidate,
        candidates: List[_SearchCandidate],
        title_norm: str,
        norm_art: str,
    ) -> _SearchCandidate:
        """Se la preferenza prefer_album è impostata e best non la rispetta, cerca un'alternativa equivalente."""
        want_album  = self.prefer_album and best.edition.is_short_form
        want_single = not self.prefer_album and best.edition.kind.value == "album"
        if not want_album and not want_single:
            return best

        best_title_norm  = TextCleaner.normalize(TextCleaner.clean_title(best.item.get("trackName", "")))
        best_artist_norm = TextCleaner.normalize(best.item.get("artistName", ""))
        target_is_short  = want_single

        alt, alt_score = None, -1.0
        for c in candidates:
            is_short = c.edition.is_short_form
            if is_short != target_is_short:
                continue
            it_norm = TextCleaner.normalize(TextCleaner.clean_title(c.item.get("trackName", "")))
            ia_norm = TextCleaner.normalize(c.item.get("artistName", ""))
            if (
                TextCleaner.title_similarity(best_title_norm, it_norm) > 0.9
                and TextCleaner.title_similarity(best_artist_norm, ia_norm) > 0.8
                and c.score > alt_score
            ):
                alt, alt_score = c, c.score

        return alt if alt else best

    # ------------------------------------------------------------------
    # Album resolution (usata dalla strategia album-hint)
    # ------------------------------------------------------------------
    def best_album_id(self, album_results: List[Dict], hint_album_norm: str, art_primary: str) -> Optional[int]:
        best_sim, best_id, best_name_norm, best_edition_overlap = -1.0, None, "", 0
        hint_editions = EditionTokens.findall(hint_album_norm)

        for alb in album_results:
            alb_artist = alb.get("collectionArtistName") or alb.get("artistName", "")
            if art_primary and alb_artist:
                norm_primary = TextCleaner.normalize(art_primary)
                norm_alb_art = TextCleaner.normalize(alb_artist)
                artist_sim = TextCleaner.title_similarity(norm_primary, norm_alb_art)
                if not (artist_sim >= 0.7 or norm_primary in norm_alb_art or norm_alb_art in norm_primary):
                    continue

            alb_name_norm   = TextCleaner.normalize(alb.get("collectionName", ""))
            cand_editions   = EditionTokens.findall(alb_name_norm)
            edition_overlap = len(hint_editions & cand_editions)

            edition_sim = TextCleaner.album_edition_similarity(hint_album_norm, alb_name_norm)
            edition_mismatch_penalty = 0.2 if (hint_editions != cand_editions) else 0.0
            sim = max(0.0, edition_sim - edition_mismatch_penalty)

            min_sim = 0.70 if hint_editions else self.SIM_ALBUM_MIN
            if sim <= min_sim:
                continue

            is_better = (
                sim > best_sim + 0.05
                or (abs(sim - best_sim) <= 0.05 and edition_overlap > best_edition_overlap)
                or (
                    abs(sim - best_sim) <= 0.05
                    and edition_overlap == best_edition_overlap
                    and len(alb_name_norm) > len(best_name_norm)
                )
            )
            if is_better:
                best_sim, best_id, best_name_norm, best_edition_overlap = (
                    sim, alb.get("collectionId"), alb_name_norm, edition_overlap
                )

        return best_id