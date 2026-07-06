CREATE TABLE AppleMusicAlbum (
    CollectionId BIGINT PRIMARY KEY,
    WrapperType VARCHAR(50),
    CollectionType VARCHAR(50),
    ArtistId BIGINT,
    ArtistName VARCHAR(255),
    CollectionName VARCHAR(255),
    CollectionViewUrl TEXT,
    CollectionExplicitness VARCHAR(20),
    TrackCount INT,
    Country VARCHAR(10),
    ReleaseDate DATETIME,
    PrimaryGenreName VARCHAR(100)
);


CREATE TABLE AppleMusicTracks (
    ArtistId BIGINT,
    CollectionId BIGINT,
    TrackId BIGINT PRIMARY KEY,
    ArtistName VARCHAR(255),
    CollectionName VARCHAR(255),
    TrackName VARCHAR(255),
    CollectionArtistName VARCHAR(255),
    ArtistViewUrl TEXT,
    CollectionViewUrl TEXT,
    ArtworkUrl TEXT, #fare un parsing e mettere 1000x100
    CollectionPrice DECIMAL(10,2),
    TrackExplicitness VARCHAR(20),
    DiscCount INT,
    DiscNumber INT,
    TrackCount INT,
    TrackNumber INT,
    TrackTimeMillis INT,
    PrimaryGenreName VARCHAR(100),
);

-- Migration: aggiungi colonna ISRC a AppleMusicTracks
ALTER TABLE AppleMusicTracks
    ADD COLUMN IF NOT EXISTS ISRC VARCHAR(12);
 
CREATE INDEX IF NOT EXISTS ix_AppleMusicTracks_ISRC
    ON AppleMusicTracks (ISRC)
    WHERE ISRC IS NOT NULL;
 
