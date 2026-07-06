from typing import Optional, Sequence

from sqlalchemy import select, inspect
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from Database.Model.ItunesModel import AppleMusicTrack
from Database.Service.BaseService import BaseService


class TrackService(BaseService):

    # ---------- READ ----------

    @staticmethod
    async def get(
        session: AsyncSession, track_id: int
    ) -> Optional[AppleMusicTrack]:
        stmt = select(AppleMusicTrack).where(
            AppleMusicTrack.track_id == track_id
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    @staticmethod
    async def get_by_album(
        session: AsyncSession,
        collection_id: int,
        with_album: bool = False,
    ) -> Sequence[AppleMusicTrack]:
        stmt = select(AppleMusicTrack).where(
            AppleMusicTrack.collection_id == collection_id
        )
        if with_album:
            stmt = stmt.options(selectinload(AppleMusicTrack.album))
        result = await session.execute(stmt)
        return result.scalars().all()

    # ---------- WRITE (single) ----------

    @staticmethod
    async def save(
        session: AsyncSession,
        track: AppleMusicTrack,
        *,
        skip_if_readonly: bool = False,
    ) -> AppleMusicTrack:
        """
        Upsert di una singola istanza.

        Usa INSERT ... ON DUPLICATE KEY UPDATE invece di session.merge():
        merge() fa SELECT poi INSERT/UPDATE, non atomico tra sessioni
        concorrenti (un thread/task per song) → rischio Duplicate entry.

        `skip_if_readonly`: se True, non scrive nulla e ritorna l'istanza
        passata inalterata. Usato dal pipeline quando il dato proviene da
        un DB-hit (ITunesResult.from_db / PipelineContext.db_hit_readonly):
        una song già letta dalla cache locale non deve mai rigenerare una
        scrittura sulla stessa riga, anche se il dato risulta "nuovo" per
        un provider remoto chiamato per errore a valle.
        """
        if skip_if_readonly:
            return track
        await TrackService.bulk_upsert(session, [track])
        refreshed = await TrackService.get(session, track.track_id)
        return refreshed or track

    # ---------- WRITE (bulk) ----------

    @staticmethod
    async def bulk_upsert(
        session: AsyncSession,
        tracks: Sequence[AppleMusicTrack],
        *,
        skip_if_readonly: bool = False,
    ) -> int:
        """
        Upsert batch via INSERT ... ON DUPLICATE KEY UPDATE (dialect MySQL).

        IMPORTANTE: dialects.mysql.insert(Model) è un construct Core, NON
        ORM-aware. .values() deve usare nomi COLONNA DB (es. "ArtistId"),
        non attributi Python (es. "artist_id") — altrimenti SQLAlchemy
        ignora silenziosamente le chiavi non riconosciute (SAWarning) e
        l'INSERT/ON DUPLICATE finisce vuoto o malformato.
        Stessa regola per stmt.inserted[...] e per il dict in
        on_duplicate_key_update(...): tutto in nomi colonna DB.

        `skip_if_readonly`: no-op esplicito, vedi `save()`.
        """
        if skip_if_readonly or not tracks:
            return 0

        mapper = inspect(AppleMusicTrack)
        _SKIP_ATTR = {"track_id"}

        col_attrs = [a for a in mapper.column_attrs if a.key not in _SKIP_ATTR]
        attr_to_col = {a.key: a.expression.name for a in col_attrs}
        pk_col = "TrackId"

        rows = [
            {attr_to_col[a.key]: getattr(track, a.key) for a in col_attrs}
            | {pk_col: track.track_id}
            for track in tracks
        ]

        stmt = insert(AppleMusicTrack).values(rows)
        stmt = stmt.on_duplicate_key_update(
            {col: stmt.inserted[col] for col in attr_to_col.values()}
        )

        await session.execute(stmt)
        await session.commit()
        return len(rows)

    # ---------- DELETE ----------

    @staticmethod
    async def delete(session: AsyncSession, track_id: int) -> bool:
        track = await TrackService.get(session, track_id)
        if not track:
            return False
        await session.delete(track)
        await session.commit()
        return True

    @staticmethod
    async def delete_by_album(
        session: AsyncSession, collection_id: int
    ) -> int:
        """Elimina tutte le tracce di un album. Ritorna il count eliminato."""
        tracks = await TrackService.get_by_album(session, collection_id)
        count = len(tracks)
        for track in tracks:
            await session.delete(track)
        await session.commit()
        return count