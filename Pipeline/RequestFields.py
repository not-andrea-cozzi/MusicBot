class RequestField:

    ITUNES_FIELDS = frozenset({
        "title", "artist", "album", "album_artist",
        "year", "track_number", "disc_number",
        "genre", "explicit", "cover_url",
        "itunes_track_id", "itunes_artist_id", "itunes_collection_id",
        "preview_url", "track_time_ms", "compilation", "copyright",
        "composer", "sort_title", "sort_artist", "sort_album", "sort_album_artist",
    })

    MB_FIELDS = frozenset({
        "title", "artist", "album", "year", "track_number", "disc_number",
        "mb_track_id", "mb_album_id", "mb_artist_id", "mb_album_artist_id",
        "mb_release_group_id", "cover_url", "genre", "label", "country",
        "isrc", "composer",
    })

    ACOUSTID_FIELDS = frozenset({
        "title", "artist", "album", "isrc",
        "mb_track_id", "mb_release_group_id", "track_time_ms",
    })

    SEED_PRESERVED_FIELDS = frozenset({
        "video_id", "preview_url",
    })