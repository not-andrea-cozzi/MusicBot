import io
import threading
from collections import OrderedDict
from typing import Optional

import httpx

try:
    from PIL import Image
    _PILLOW_OK = True
except ImportError:
    _PILLOW_OK = False

_UA = "ScariMusicBot/5.0 (your-email@example.com)"


class CoverManager:
    def __init__(self, maxsize: int = 200, timeout: float = 15.0) -> None:
        self.maxsize = maxsize
        self._cache: OrderedDict[str, bytes] = OrderedDict()
        self._lock = threading.Lock()
        self._client = httpx.Client(
            headers={"User-Agent": _UA},
            timeout=timeout,
            follow_redirects=True,
        )

    def _get(self, url: str) -> Optional[bytes]:
        with self._lock:
            if url in self._cache:
                self._cache.move_to_end(url)
                return self._cache[url]
        return None

    def _set(self, url: str, data: bytes) -> None:
        with self._lock:
            if url in self._cache:
                self._cache.move_to_end(url)
            else:
                if len(self._cache) >= self.maxsize:
                    self._cache.popitem(last=False)
                self._cache[url] = data

    def fetch(self, url: str) -> Optional[bytes]:
        if not url:
            return None
        cached = self._get(url)
        if cached is not None:
            return cached
        try:
            r = self._client.get(url)
            data = (
                r.content
                if r.status_code == 200 and "image" in r.headers.get("content-type", "")
                else None
            )
        except Exception:
            data = None
        if data:
            self._set(url, data)
        return data

    def resize(self, data: bytes, size: int = 1500) -> bytes:
        if not _PILLOW_OK or not data:
            return data
        try:
            img = Image.open(io.BytesIO(data)).convert("RGB")
            
            # thumbnail ridimensiona l'immagine in modo che il lato più lungo 
            # sia al massimo 'size', mantenendo intatte le proporzioni originali.
            # Non effettua alcun ritaglio forzato.
            img.thumbnail((size, size), Image.LANCZOS)
            
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=90)
            return buf.getvalue()
        except Exception:
            return data

    def fetch_and_resize(self, url: str) -> Optional[bytes]:
        data = self.fetch(url)
        return self.resize(data) if data else None

    def fetch_cascade(
        self,
        mbid: str = "",
        rgid: str = "",
        ytdlp_url: str = "",
        meta_cover_url: str = "",
    ) -> Optional[bytes]:
        """
        Priorità:
          1. meta_cover_url  (iTunes/Deezer — escluso thumbnail YouTube)
          2. Cover Art Archive release-group
          3. Cover Art Archive release
          4. ytdlp_url (thumbnail YouTube, last resort)
        """
        candidates = filter(None, [
            meta_cover_url if meta_cover_url and not meta_cover_url.startswith("https://i.ytimg.com") else None,
            rgid and f"https://coverartarchive.org/release-group/{rgid}/front",
            mbid and f"https://coverartarchive.org/release/{mbid}/front",
            ytdlp_url,
        ])
        for url in candidates:
            result = self.fetch_and_resize(url)
            if result:
                return result
        return None

    def close(self) -> None:
        self._client.close()