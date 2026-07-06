import os

import yt_dlp

from Model.Song import Song


class Downloader:
    """Gestisce il download audio tramite yt-dlp."""

    _YDL_BASE_OPTS = {
        "format": "bestaudio[ext=m4a]/bestaudio[ext=aac]/bestaudio/best",
        "extractor_args": {"youtube": ["player_client:android,web_creator,ios"]},
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "m4a"}],
        "postprocessor_args": {"ExtractAudio": ["-map_metadata", "-1", "-vn"]},
        "noplaylist": False,
        "ignoreerrors": True,
        "nooverwrites": True,
        "continuedl": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
    }

    def __init__(self, output_dir: str, logger) -> None:
        self.output_dir = output_dir
        self.logger = logger

    def download_url(self, url: str) -> list["Song"]:
        real_paths: dict[str, str] = {}

        def _pp_hook(info: dict) -> None:
            if info.get("status") == "finished":
                vid = info.get("info_dict", {}).get("id") or info.get("id", "")
                fpath = info.get("info_dict", {}).get("filepath") or info.get("filepath", "")
                if vid and fpath:
                    real_paths[vid] = fpath

        opts = {
            **self._YDL_BASE_OPTS,
            "outtmpl": os.path.join(self.output_dir, "%(id)s.%(ext)s"),
            "postprocessor_hooks": [_pp_hook],
        }

        songs: list[Song] = []
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if not info:
                    self.logger.warning(f"[Downloader] Nessuna informazione per {url}")
                    return []

                for e in info.get("entries") or [info]:
                    if not e:
                        continue
                    
                    vid = e.get("id", "")
                    real = ""

                    req_downloads = e.get("requested_downloads")
                    if req_downloads and isinstance(req_downloads, list):
                        real = req_downloads[0].get("filepath", "")

                    if not real or not os.path.isfile(real):
                        real = real_paths.get(vid) or ""

                    if not real or not os.path.isfile(real):
                        real = os.path.splitext(ydl.prepare_filename(e))[0] + ".m4a"

                    raw_title = e.get("title", "")
                    raw_artist = e.get("artist") or ""
                    
                  
                    if not raw_artist and " - " not in raw_title:
                        raw_artist = e.get("uploader") or ""

                    song = Song(video_id=vid)
                    song.path_original = real
                    song.set_path(real)
                    song.raw = {
                        "title":          raw_title,
                        "artist":         raw_artist,
                        "album":          e.get("album") or "",
                        "release_year":   e.get("release_year"),
                        "upload_date":    e.get("upload_date"),
                        "track_number":   e.get("track_number"),
                        "thumbnail":      e.get("thumbnail") or "",
                        "isrc":           e.get("isrc") or "",
                        "video_id":       vid,
                        "playlist_id":    e.get("playlist_id") or info.get("id") or "",
                        "playlist_title": e.get("playlist_title") or info.get("title") or "",
                    }
                    songs.append(song)
                    self.logger.debug(f"[Downloader] Trovato/Scaricato: {vid} -> {real}")
        except Exception as exc:
            self.logger.error(f"[Downloader] Errore per {url}: {exc}")
        return songs