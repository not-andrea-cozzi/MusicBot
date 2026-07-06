from __future__ import annotations

import os
import threading
import logging

from Algorithm.TextCleaner import TextCleaner


class SongFileManager:
    """
    Gestione statica dei file audio su disco: solo rinomina/collision-handling.
    La scrittura dei tag è delegata a SongMetaWriter.
    """

    _MAX_ARTIST_LEN = 100
    _rename_lock = threading.Lock()

    log = logging.getLogger(__name__)

    @classmethod
    def rename_file(cls, path: str, meta: dict, output_dir: str) -> str:
        title = cls._safe_str(meta.get("title"))
        if not title or not os.path.isfile(path):
            return path

        safe_title = TextCleaner.sanitize_filename(title)

        with cls._rename_lock:
            if not os.path.isfile(path):
                return path

            candidates = [os.path.join(output_dir, f"{safe_title}.m4a")]

            artist  = cls._safe_str(meta.get("artist").replace(";", ","), max_len=cls._MAX_ARTIST_LEN)
            primary = TextCleaner.sanitize_filename(TextCleaner.primary_artist(artist)) if artist else ""
            if primary:
                candidates.append(os.path.join(output_dir, f"{safe_title} - {primary}.m4a"))

            base = candidates[-1]
            name, ext = os.path.splitext(os.path.basename(base))
            candidates += [os.path.join(output_dir, f"{name} ({i}){ext}") for i in range(1, 11)]

            for dst in candidates:
                if cls._same_path(path, dst):
                    return path
                if not os.path.exists(dst):
                    return cls._do_rename(path, dst)

        cls.log.warning(f"[Rinomina] Collisione irrisolvibile: {os.path.basename(path)}")
        return path

    @classmethod
    def _do_rename(cls, src: str, dst: str) -> str:
        try:
            os.replace(src, dst)
            cls.log.info(f"[Rinomina] → {os.path.basename(dst)}")
            return dst
        except OSError as exc:
            cls.log.error(f"[Rinomina] {exc}")
            return src

    @staticmethod
    def _same_path(a: str, b: str) -> bool:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))

    @staticmethod
    def _safe_str(value, max_len: int = 1000):
        if value is None:
            return None
        s = str(value).strip()
        return s[:max_len] if s else None