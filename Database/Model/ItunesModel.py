import re
from datetime import datetime
from typing import List, Optional
from sqlalchemy import BigInteger, String, Text, Integer, DateTime, Numeric, ForeignKey, Index
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class AppleMusicAlbum(Base):
    __tablename__ = 'AppleMusicAlbum'

    collection_id:             Mapped[int]           = mapped_column("CollectionId", BigInteger, primary_key=True)
    wrapper_type:              Mapped[Optional[str]]  = mapped_column("WrapperType", String(50))
    collection_type:           Mapped[Optional[str]]  = mapped_column("CollectionType", String(50))
    artist_id:                 Mapped[Optional[int]]  = mapped_column("ArtistId", BigInteger)
    artist_name:               Mapped[Optional[str]]  = mapped_column("ArtistName", String(255))
    collection_name:           Mapped[Optional[str]]  = mapped_column("CollectionName", String(255))
    collection_view_url:       Mapped[Optional[str]]  = mapped_column("CollectionViewUrl", Text)
    collection_explicitness:   Mapped[Optional[str]]  = mapped_column("CollectionExplicitness", String(20))
    track_count:               Mapped[Optional[int]]  = mapped_column("TrackCount", Integer)
    country:                   Mapped[Optional[str]]  = mapped_column("Country", String(10))
    release_date:              Mapped[Optional[datetime]] = mapped_column("ReleaseDate", DateTime)
    primary_genre_name:        Mapped[Optional[str]]  = mapped_column("PrimaryGenreName", String(100))

    tracks: Mapped[List["AppleMusicTrack"]] = relationship(
        back_populates="album",
        cascade="all, delete-orphan",
    )


class AppleMusicTrack(Base):
    __tablename__ = 'AppleMusicTracks'

    track_id:               Mapped[int]           = mapped_column("TrackId", BigInteger, primary_key=True)
    artist_id:              Mapped[Optional[int]]  = mapped_column("ArtistId", BigInteger)
    collection_id:          Mapped[Optional[int]]  = mapped_column(
        "CollectionId", BigInteger, ForeignKey("AppleMusicAlbum.CollectionId")
    )
    artist_name:            Mapped[Optional[str]]  = mapped_column("ArtistName", String(255))
    collection_name:        Mapped[Optional[str]]  = mapped_column("CollectionName", String(255))
    track_name:             Mapped[Optional[str]]  = mapped_column("TrackName", String(255))
    collection_artist_name: Mapped[Optional[str]]  = mapped_column("CollectionArtistName", String(255))
    _artwork_url:           Mapped[Optional[str]]  = mapped_column("ArtworkUrl", Text)
    track_explicitness:     Mapped[Optional[str]]  = mapped_column("TrackExplicitness", String(20))
    disc_count:             Mapped[Optional[int]]  = mapped_column("DiscCount", Integer)
    disc_number:            Mapped[Optional[int]]  = mapped_column("DiscNumber", Integer)
    track_count:            Mapped[Optional[int]]  = mapped_column("TrackCount", Integer)
    track_number:           Mapped[Optional[int]]  = mapped_column("TrackNumber", Integer)
    track_time_millis:      Mapped[Optional[int]]  = mapped_column("TrackTimeMillis", Integer)
    primary_genre_name:     Mapped[Optional[str]]  = mapped_column("PrimaryGenreName", String(100))
    isrc:                   Mapped[Optional[str]]  = mapped_column("ISRC", String(12), index=True)

    __table_args__ = (
        Index("ix_AppleMusicTracks_ISRC", "ISRC"),
    )

    album: Mapped["AppleMusicAlbum"] = relationship(back_populates="tracks")

    @property
    def artwork_url(self) -> Optional[str]:
        return self._artwork_url

    @artwork_url.setter
    def artwork_url(self, value: Optional[str]):
        if value:
            self._artwork_url = re.sub(r"/\d+x\d+bb\.jpg$", "/1000x1000bb.jpg", value)
        else:
            self._artwork_url = value