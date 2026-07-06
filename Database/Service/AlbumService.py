from typing import Optional, Sequence

from sqlalchemy import select, inspect
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from Database.Model.ItunesModel import AppleMusicAlbum
from Database.Service.BaseService import BaseService


class AlbumService(BaseService):

    # ---------- READ ----------

    @staticmethod
    async def get(
        session: AsyncSession,
        collection_id: int,
        with_tracks: bool = False,
    ) -> Optional[AppleMusicAlbum]:
        stmt = select(AppleMusicAlbum).where(
            AppleMusicAlbum.collection_id == collection_id
        )
        if with_tracks:
            stmt = stmt.options(selectinload(AppleMusicAlbum.tracks))
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    @staticmethod
    async def get_all(
        session: AsyncSession,
        limit: int = 100,
        offset: int = 0,
        with_tracks: bool = False,
    ) -> Sequence[AppleMusicAlbum]:
        stmt = select(AppleMusicAlbum).limit(limit).offset(offset)
        if with_tracks:
            stmt = stmt.options(selectinload(AppleMusicAlbum.tracks))
        result = await session.execute(stmt)
        return result.scalars().all()

    # ---------- WRITE (single) ----------

    @staticmethod
    async def save(
        session: AsyncSession,
        album: AppleMusicAlbum,
        *,
        skip_if_readonly: bool = False,
    ) -> AppleMusicAlbum:
        """
        Upsert di una singola istanza.

        Usa INSERT ... ON DUPLICATE KEY UPDATE (atomico lato DB) invece di
        session.merge(): con più sessioni concorrenti (un thread/task per
        song) due merge() sullo stesso collection_id possono entrambi
        vedere "non esiste" e tentare INSERT, causando 1062 Duplicate entry.

        `skip_if_readonly`: vedi TrackService.save — propagato dal pipeline
        quando l'album proviene da un DB-hit e non deve essere riscritto.
        """
        if skip_if_readonly:
            return album
        await AlbumService.bulk_upsert(session, [album])
        refreshed = await AlbumService.get(session, album.collection_id)
        return refreshed or album

    # ---------- WRITE (bulk) ----------

    @staticmethod
    async def bulk_upsert(
        session: AsyncSession,
        albums: Sequence[AppleMusicAlbum],
        *,
        skip_if_readonly: bool = False,
    ) -> int:
        """
        Upsert batch via INSERT ... ON DUPLICATE KEY UPDATE (dialect MySQL).

        IMPORTANTE: dialects.mysql.insert(Model) è Core, non ORM-aware.
        .values() deve usare nomi COLONNA DB, non attributi Python.
        """
        if skip_if_readonly or not albums:
            return 0

        mapper = inspect(AppleMusicAlbum)
        _SKIP_ATTR = {"collection_id"}

        col_attrs = [a for a in mapper.column_attrs if a.key not in _SKIP_ATTR]
        attr_to_col = {a.key: a.expression.name for a in col_attrs}
        pk_col = "CollectionId"

        rows = [
            {attr_to_col[a.key]: getattr(album, a.key) for a in col_attrs}
            | {pk_col: album.collection_id}
            for album in albums
        ]

        stmt = insert(AppleMusicAlbum).values(rows)
        stmt = stmt.on_duplicate_key_update(
            {col: stmt.inserted[col] for col in attr_to_col.values()}
        )

        await session.execute(stmt)
        await session.commit()
        return len(rows)

    # ---------- UPDATE (parziale) ----------

    @staticmethod
    async def update(
        session: AsyncSession,
        collection_id: int,
        data: dict,
    ) -> Optional[AppleMusicAlbum]:
        album = await AlbumService.get(session, collection_id)
        if not album:
            return None
        for key, value in data.items():
            if key != "collection_id" and hasattr(album, key):
                setattr(album, key, value)
        await BaseService._commit_refresh(session, album)
        return album

    # ---------- DELETE ----------

    @staticmethod
    async def delete(session: AsyncSession, collection_id: int) -> bool:
        album = await AlbumService.get(session, collection_id)
        if not album:
            return False
        await session.delete(album)
        await session.commit()
        return True