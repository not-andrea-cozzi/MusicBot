import os
from dotenv import load_dotenv


class ConfigLoader:
    """
    Carica la configurazione da un file .env mantenendo
    la compatibilità con il vecchio schema YAML.
    """

    def __init__(self, env_path: str = ".env"):
        self.env_path = env_path
        self._load_env()

    def _load_env(self):
        if not os.path.exists(self.env_path):
            print(
                f"[ConfigLoader] Avviso: File '{self.env_path}' non trovato. "
                "Verranno usate le variabili d'ambiente del sistema."
            )
            return

        load_dotenv(self.env_path)

    def get(self, key: str, default=None):
        return os.getenv(key, default)

    def get_bool(self, key: str, default: bool = False) -> bool:
        value = os.getenv(key)
        if value is None:
            return default

        return value.lower() in (
            "true",
            "1",
            "yes",
            "y",
            "on"
        )

    # ------------------------------------------------------------------
    # APP
    # ------------------------------------------------------------------

    @property
    def app(self) -> dict:
        return {
            "name": self.get("APP_NAME"),
            "country": self.get("APP_DOWNLOAD_COUNTRY", "US"),
            "version": self.get("APP_VERSION"),
            "debug": self.get_bool("APP_DEBUG", False),
            "output_dir": self.get("APP_OUTPUT_DIR", "test_itunes"),
            "input_file": self.get("APP_INPUT_FILE", "input.txt"),
            "download_workers": int(self.get("APP_DOWNLOAD_WORKERS", 3)),
            "tag_workers": int(self.get("APP_TAG_WORKERS", 8)),
        }

    # ------------------------------------------------------------------
    # NETWORK
    # ------------------------------------------------------------------

    @property
    def network(self) -> dict:
        return {
            "user_agent": self.get(
                "NETWORK_USER_AGENT",
                "ScariMusicBot/5.0"
            ),
            "timeout": int(self.get("NETWORK_TIMEOUT", 30)),
            "retries": int(self.get("NETWORK_RETRIES", 3)),

            "itunes_min_interval": float(
                self.get("NETWORK_ITUNES_MIN_INTERVAL", 1.0)
            ),

            "itunes_fallback_countries": [
                x.strip()
                for x in self.get(
                    "NETWORK_ITUNES_FALLBACK_COUNTRIES",
                    "GB,CA"
                ).split(",")
                if x.strip()
            ],

            "cover_timeout": int(
                self.get("NETWORK_COVER_TIMEOUT", 30)
            ),

            "download_timeout": int(
                self.get("NETWORK_DOWNLOAD_TIMEOUT", 1000)
            ),

            "tag_timeout": int(
                self.get("NETWORK_TAG_TIMEOUT", 120)
            ),

            "musicbrainz_base": self.get(
                "MUSICBRAINZ_BASE",
                "https://musicbrainz.org/ws/2"
            ),
        }

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------

    @property
    def api(self) -> dict:
        return {
            "spotify_client_id": self.get(
                "SPOTIFY_CLIENT_ID", ""
            ),

            "spotify_client_secret": self.get(
                "SPOTIFY_CLIENT_SECRET", ""
            ),

            "acoustid_api_key": self.get(
                "ACOUSTID_API_KEY", ""
            ),
        }

    # ------------------------------------------------------------------
    # SCORING
    # ------------------------------------------------------------------

    @property
    def scoring(self) -> dict:
        return {
            "threshold": float(
                self.get("SCORING_THRESHOLD", 0.5)
            ),

            "itunes_prefer_album": self.get_bool(
                "ITUNES_PREFER_ALBUM",
                False
            ),

            "itunes_prefer_explicit": self.get_bool(
                "ITUNES_PREFER_EXPLICIT",
                True
            ),

            "ytmusic_accept": float(
                self.get("YTMUSIC_ACCEPT", 0.80)
            ),

            "ytmusic_fallback": float(
                self.get("YTMUSIC_FALLBACK", 0.65)
            ),

            "acoustid_min_score": float(
                self.get("ACOUSTID_MIN_SCORE", 0.70)
            ),
        }