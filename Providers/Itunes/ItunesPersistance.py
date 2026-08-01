from __future__ import annotations

import logging
from typing import Dict, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from Database.Model.ItunesModel import AppleMusicAlbum, AppleMusicTrack
from Database.Service.AlbumService import AlbumService
from Database.Service.TrackService import TrackService


class ITunesPersistence:
    """Scrittura su DB locale dei risultati iTunes: album, tracce, ISRC."""

    def __init__(
        self,
        db_session: Optional[AsyncSession] = None,
        logger: Optional[logging.Logger] = None,
        country: str = "US",
    ) -> None:
        self.db_session = db_session
        self.logger = logger or logging.getLogger(__name__)
        self.country = country

    @staticmethod
    def album_record_from_track(track: Dict) -> Optional[Dict]:
        cid = track.get("collectionId")
        if not cid:
            return None
        return {
            "wrapperType": "collection", "collectionId": cid,
            "collectionType": track.get("collectionType"), "artistId": track.get("artistId"),
            "artistName": track.get("collectionArtistName") or track.get("artistName"),
            "collectionName": track.get("collectionName"), "collectionViewUrl": track.get("collectionViewUrl"),
            "collectionExplicitness": track.get("collectionExplicitness"), "trackCount": track.get("trackCount"),
            "country": track.get("country"), "primaryGenreName": track.get("primaryGenreName"),
        }

    async def persist_lookup_results(self, results: list, known_isrc: str = "") -> None:
        if not self.db_session or not results:
            return
        try:
            album_dicts = [r for r in results if r.get("wrapperType") == "collection" and r.get("collectionId")]
            track_dicts = [r for r in results if r.get("wrapperType") == "track" and r.get("trackId")]

            for a in album_dicts:
                album = AppleMusicAlbum(
                    collection_id=a["collectionId"], wrapper_type=a.get("wrapperType"),
                    collection_type=a.get("collectionType") or "Album", artist_id=a.get("artistId"),
                    artist_name=a.get("artistName"), collection_name=a.get("collectionName"),
                    collection_view_url=a.get("collectionViewUrl"),
                    collection_explicitness=a.get("collectionExplicitness"),
                    track_count=a.get("trackCount"), country=a.get("country", self.country),
                    primary_genre_name=a.get("primaryGenreName"),
                )
                await AlbumService.save(self.db_session, album)

            if track_dicts:
                tracks = []
                for t in track_dicts:
                    track = AppleMusicTrack(
                        track_id=t["trackId"], artist_id=t.get("artistId"), collection_id=t.get("collectionId"),
                        artist_name=t.get("artistName"), collection_name=t.get("collectionName"),
                        track_name=t.get("trackName"), collection_artist_name=t.get("collectionArtistName"),
                        track_explicitness=t.get("trackExplicitness"), disc_count=t.get("discCount"),
                        disc_number=t.get("discNumber"), track_count=t.get("trackCount"),
                        track_number=t.get("trackNumber"), track_time_millis=t.get("trackTimeMillis"),
                        primary_genre_name=t.get("primaryGenreName"),
                        isrc=known_isrc.upper() if known_isrc else None,
                    )
                    track.artwork_url = t.get("artworkUrl100")
                    tracks.append(track)
                await TrackService.bulk_upsert(self.db_session, tracks)
        except Exception as exc:
            self.logger.warning(f"[iTunes] Errore persistenza lookup results: {exc}", exc_info=True)

    async def persist_best_result(self, item: Optional[Dict], known_isrc: str = "") -> None:
        if not self.db_session or not item:
            return
        if item.get("wrapperType") != "track" or not item.get("trackId"):
            return
        album_record = self.album_record_from_track(item)
        if album_record:
            await self.persist_lookup_results([album_record], known_isrc=known_isrc)
        await self.persist_lookup_results([item], known_isrc=known_isrc)

    async def persist_track_isrc(self, track_id: int, isrc: str) -> None:
        """Aggiorna l'ISRC su una traccia già persistita. Non sovrascrive se già presente."""
        if not self.db_session or not track_id or not isrc:
            return
        try:
            from sqlalchemy import update
            stmt = (
                update(AppleMusicTrack)
                .where(AppleMusicTrack.track_id == track_id)
                .where(AppleMusicTrack.isrc.is_(None))
                .values(isrc=isrc.upper())
            )
            await self.db_session.execute(stmt)
            await self.db_session.commit()
            self.logger.debug(f"[iTunes] ISRC {isrc!r} → track_id={track_id}")
        except Exception as exc:
            self.logger.debug(f"[iTunes] persist_track_isrc fallito: {exc}")