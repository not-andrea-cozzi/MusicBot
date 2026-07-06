import os
import logging
from typing import Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class NetworkClient:

    DEFAULT_TIMEOUT = (10, 30)  # (connect, read)

    def __init__(
        self,
        user_agent: str,
        timeout: Tuple[float, float] = DEFAULT_TIMEOUT,
        retry_total: int = 5,
        retry_backoff: float = 2.0,
        retry_status_forcelist: Tuple[int, ...] = (403, 429, 500, 502, 503, 504),
        logger: Optional[logging.Logger] = None,
    ):
        self.timeout = timeout
        self.logger = logger or logging.getLogger(__name__)

        retry_strategy = Retry(
            total=retry_total,
            backoff_factor=retry_backoff,
            status_forcelist=retry_status_forcelist,
            allowed_methods={"GET"},
            raise_on_status=False,
        )

        adapter = HTTPAdapter(
            max_retries=retry_strategy,
            pool_connections=10,
            pool_maxsize=20,
        )
        self.session = requests.Session()
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers.update({
            "User-Agent": user_agent,
            "Accept-Encoding": "gzip, deflate",   
        })

    def get(self, url: str, **kwargs) -> Optional[requests.Response]:
        """GET con timeout di default; ritorna None su errore, loggandolo."""
        kwargs.setdefault("timeout", self.timeout)
        try:
            return self.session.get(url, **kwargs)
        except requests.RequestException as e:
            self.logger.debug(f"GET fallita: {url} - {e}")
            return None

    def close(self):
        """Chiude la sessione HTTP."""
        self.session.close()