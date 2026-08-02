from typing import Optional, Sequence

from sqlalchemy import select, inspect
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from Database.Model.ItunesModel import AppleMusicArtist
from Database.Service.BaseService import BaseService


class ArtistService(BaseService):

    # ---------- READ ----------

    @staticmethod
    async def get(session: AsyncSession, artist_id: int) -> Optional[AppleMusicArtist]:
        stmt = select(AppleMusicArtist).where(AppleMusicArtist.artist_id == artist_id)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    @staticmethod
    async def get_many(session: AsyncSession, artist_ids: Sequence[int]) -> Sequence[AppleMusicArtist]:
        if not artist_ids:
            return []
        stmt = select(AppleMusicArtist).where(AppleMusicArtist.artist_id.in_(artist_ids))
        result = await session.execute(stmt)
        return result.scalars().all()

    @staticmethod
    async def get_all(session: AsyncSession, limit: int = 100, offset: int = 0) -> Sequence[AppleMusicArtist]:
        stmt = select(AppleMusicArtist).limit(limit).offset(offset)
        result = await session.execute(stmt)
        return result.scalars().all()

    # ---------- WRITE (single) ----------

    @staticmethod
    async def save(
        session: AsyncSession,
        artist: AppleMusicArtist,
        *,
        skip_if_readonly: bool = False,
    ) -> AppleMusicArtist:
        if skip_if_readonly:
            return artist
        await ArtistService.bulk_upsert(session, [artist])
        refreshed = await ArtistService.get(session, artist.artist_id)
        return refreshed or artist

    # ---------- WRITE (bulk) ----------

    @staticmethod
    async def bulk_upsert(
        session: AsyncSession,
        artists: Sequence[AppleMusicArtist],
        *,
        skip_if_readonly: bool = False,
    ) -> int:
        """Upsert batch via INSERT ... ON DUPLICATE KEY UPDATE (dialect MySQL). Stessa regola di AlbumService/TrackService: .values() in nomi colonna DB, non attributi Python."""
        if skip_if_readonly or not artists:
            return 0

        mapper = inspect(AppleMusicArtist)
        _SKIP_ATTR = {"artist_id"}

        col_attrs = [a for a in mapper.column_attrs if a.key not in _SKIP_ATTR]
        attr_to_col = {a.key: a.expression.name for a in col_attrs}
        pk_col = "ArtistId"

        rows = [
            {attr_to_col[a.key]: getattr(artist, a.key) for a in col_attrs}
            | {pk_col: artist.artist_id}
            for artist in artists
        ]

        stmt = insert(AppleMusicArtist).values(rows)
        stmt = stmt.on_duplicate_key_update(
            {col: stmt.inserted[col] for col in attr_to_col.values()}
        )

        await session.execute(stmt)
        await session.commit()
        return len(rows)

    # ---------- UPDATE (parziale) ----------

    @staticmethod
    async def update(session: AsyncSession, artist_id: int, data: dict) -> Optional[AppleMusicArtist]:
        artist = await ArtistService.get(session, artist_id)
        if not artist:
            return None
        for key, value in data.items():
            if key != "artist_id" and hasattr(artist, key):
                setattr(artist, key, value)
        await BaseService._commit_refresh(session, artist)
        return artist

    # ---------- DELETE ----------

    @staticmethod
    async def delete(session: AsyncSession, artist_id: int) -> bool:
        artist = await ArtistService.get(session, artist_id)
        if not artist:
            return False
        await session.delete(artist)
        await session.commit()
        return True