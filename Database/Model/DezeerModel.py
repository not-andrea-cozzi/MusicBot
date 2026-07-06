from typing import Any, Dict
from sqlalchemy import BigInteger, JSON
from sqlalchemy.orm import Mapped, mapped_column

from Database.Model.ItunesModel import Base

class DeezerAlbumMeta(Base):
    __tablename__ = 'deezer_album_meta'

    album_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    payload: Mapped[Dict[str, Any]] = mapped_column(JSON)