# BotOrchestrator.py
from __future__ import annotations

import logging
import os
import re
import threading
import concurrent.futures
from typing import List

import httpx

from Providers.SpotifyProvider import SpotifyProvider
from Utils.CoverManager import CoverManager
from Utils.YTDownloader import Downloader
from Model.Song import Song
from Pipeline.MetadataPipeline import MetadataPipeline
from Providers.MusicBrainzApi import MusicBrainzApiRequstor
from Model.SongFileManager import SongFileManager
from classes.configloader import ConfigLoader
from Providers.ItunesProvider import ITunesProvider
from Providers.DeezerProvider import DeezerProvider
from classes.logger import LoggerSetup
from Pipeline.SongProcessor import SongProcessor
from Database.session import init_db


# Sostituisci l'intera classe ThreadSafePipelineWrapper in BotOrchestrator.py

class ThreadSafePipelineWrapper:
    """Crea provider + DB session isolati per ogni asyncio.run() call (un loop per song)."""

    def __init__(self, config, logger) -> None:
        self.config      = config
        self.logger      = logger
        self._db_url: str = "mysql+asyncmy://user:password@localhost:3306/songdb"



    async def run(self, song: Song) -> Song:
        db_engine, db_session = await self._make_db_session()

        try:
            async with (
                httpx.AsyncClient(timeout=10.0) as itunes_http,
                httpx.AsyncClient(headers={"User-Agent": "ScariMusicBot/5.0"}, timeout=10.0) as mb_http,
                httpx.AsyncClient(timeout=10.0) as deezer_http,
            ):
                itunes = ITunesProvider(
                    session=itunes_http,
                    logger=self.logger,
                    country=self.config.app.get("country"),
                    prefer_album=self.config.scoring.get("itunes_prefer_album", False),
                    min_request_interval=self.config.network.get("itunes_min_interval", 1.0),
                    prefer_explicit=self.config.scoring.get("itunes_prefer_explicit", True),
                    db_session=db_session,
                )

                mb_api  = MusicBrainzApiRequstor(client=mb_http)
                deezer  = DeezerProvider(client=deezer_http, logger=self.logger)
                spotify = self._build_spotify()

                pipeline = MetadataPipeline(
                    itunes=itunes,
                    mb=mb_api,
                    deezer=deezer,
                    logger=self.logger,
                    spotify=spotify,
                    use_mb=self.config.get_bool("PIPELINE_USE_MB", True),
                    use_spotify=self.config.get_bool("PIPELINE_USE_SPOTIFY", False),
                    db_session=db_session,
                )

                return await pipeline.run(song)
        finally:
            # Chiudi session e engine nello STESSO loop in cui sono stati creati
            if db_session:
                await db_session.close()
            if db_engine:
                await db_engine.dispose()

    async def _make_db_session(self):
        """
        Crea un engine + session legati al loop corrente.
        Restituisce (None, None) se DATABASE_URL non è configurato o la connessione fallisce.
        """
        if not self._db_url:
            return None, None
        try:
            from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
            engine = create_async_engine(
                self._db_url,
                echo=False,
                pool_size=1,
                max_overflow=0,
                pool_recycle=3600,
            )
            factory = async_sessionmaker(
                bind=engine, class_=AsyncSession,
                expire_on_commit=False, autoflush=False,
            )
            session = factory()
            return engine, session
        except Exception as exc:
            self.logger.debug(f"[ThreadSafePipelineWrapper] DB session fallita: {exc}")
            return None, None

    def _build_spotify(self) -> SpotifyProvider | None:
        sp_id     = self.config.api.get("spotify_client_id", "")
        sp_secret = self.config.api.get("spotify_client_secret", "")
        if not (sp_id and sp_secret):
            return None
        try:
            return SpotifyProvider(client_id=sp_id, client_secret=sp_secret, logger=self.logger)
        except Exception as exc:
            self.logger.warning(f"[ThreadSafePipelineWrapper] Spotify init fallita: {exc}")
            return None

    def close(self) -> None:
        pass

    async def aclose(self) -> None:
        pass

class BotOrchestrator:

    _GENERIC_CHANNEL = re.compile(
        r'(vevo|topic|official|channel|music|lyrics|audio|video|mix|playlist|sounds?|records?|entertainment)$',
        re.IGNORECASE,
    )

    def __init__(self) -> None:
        self.config = ConfigLoader()
        self.logger = LoggerSetup.configure(self.config.app.get("debug", True))

        self.output_dir = self.config.app.get("output_dir", "test_itunes")
        self.temp_dir   = os.path.join(self.output_dir, ".temp_download")

        self.downloader     = Downloader(self.temp_dir, self.logger)
        self.cover_manager  = CoverManager(maxsize=200)
        self.file_lock      = threading.Lock()
        self.cover_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)

        self._cover_shutdown_timeout = self.config.network.get("cover_shutdown_timeout", 60)
      

    def _make_processor(self) -> SongProcessor:
        """Crea un SongProcessor con pipeline isolata — chiamare per ogni thread."""
        return SongProcessor(
            pipeline=ThreadSafePipelineWrapper(
                config=self.config,
                logger=self.logger,
            ),
            file_manager=SongFileManager,
            cover_manager=self.cover_manager,
            output_dir=self.output_dir,
            logger=self.logger,
            cover_executor=self.cover_executor,
            cover_timeout=self.config.network.get("cover_timeout", 30),
        )

    def _process_one(self, song: Song) -> None:
        """Elabora una singola song con pipeline dedicata (thread-safe)."""
        processor = self._make_processor()
        try:
            processor.process(song)
        finally:
            processor.close()

    # ── run ──────────────────────────────────────────────────────────────────

    async def run(self) -> None:
        input_file = self.config.app.get("input_file", "input.txt")
        if not os.path.exists(input_file):
            self.logger.error(f"File di input non trovato: {input_file}")
            return
    
        urls = self._read_urls(input_file)
        if not urls:
            self.logger.info("Nessun URL da elaborare.")
            return

        os.makedirs(self.output_dir, exist_ok=True)
        os.makedirs(self.temp_dir, exist_ok=True)
        self.logger.info(f"--- Inizio download: {len(urls)} URL ---")

        unique_songs = self._download_all(urls)
        if not unique_songs:
            self.logger.info("Nessun file scaricato.")
            return

        self.logger.info(f"--- Elaborazione tag: {len(unique_songs)} file ---")
        self._tag_all(unique_songs)

        self._log_missing_summary()
        self._shutdown_cover_executor()
        self._cleanup_temp_dir()

        self.logger.info("Elaborazione completata con successo.")

    # ── steps ────────────────────────────────────────────────────────────────

    @staticmethod
    def _read_urls(input_file: str) -> List[str]:
        with open(input_file, encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip() and not line.startswith("#")]

    def _download_all(self, urls: List[str]) -> List[Song]:
        dl_workers = self.config.app.get("download_workers", 3)
        dl_timeout = self.config.network.get("download_timeout", 1000)
        all_songs: List[Song] = []

        with concurrent.futures.ThreadPoolExecutor(max_workers=dl_workers) as pool:
            futures = {pool.submit(self.downloader.download_url, u): u for u in urls}
            for future in concurrent.futures.as_completed(futures, timeout=dl_timeout * 2):
                url = futures[future]
                try:
                    all_songs.extend(future.result(timeout=dl_timeout))
                except Exception as exc:
                    self.logger.error(f"[Errore Download] {url}: {exc}")

        return self._dedupe_songs(all_songs)

    def _dedupe_songs(self, songs: List[Song]) -> List[Song]:
        seen: set[str] = set()
        unique: List[Song] = []
        for song in songs:
            if not song.path_current:
                continue
            rp = os.path.realpath(song.path_current)
            if rp in seen:
                continue
            seen.add(rp)
            unique.append(song)
        return unique

    def _tag_all(self, songs: List[Song]) -> None:
        tag_workers = self.config.app.get("tag_workers", 8)
        tag_timeout = self.config.network.get("tag_timeout", 12000)

        with concurrent.futures.ThreadPoolExecutor(max_workers=tag_workers) as pool:
            futures = {pool.submit(self._process_one, song): song for song in songs}
            for future in concurrent.futures.as_completed(futures, timeout=tag_timeout * 10):
                song = futures[future]
                try:
                    future.result(timeout=tag_timeout)
                except Exception as exc:
                    self.logger.error(f"[SongProcessor] Errore nell'elaborazione di {song.video_id}: {exc}")

   

    def _shutdown_cover_executor(self) -> None:
        self.logger.info("Attendo la chiusura del ThreadPoolExecutor delle copertine...")
        self.cover_executor.shutdown(wait=False)
        try:
            # cancel_futures non disponibile <3.9; fallback già gestito da wait=False sopra
            done, not_done = concurrent.futures.wait(
                [], timeout=0  # placeholder: usato solo se servono future espliciti
            )
        except Exception:
            pass

        # Attesa attiva con timeout esplicito invece di wait=True bloccante senza limite
        import time
        deadline = time.monotonic() + self._cover_shutdown_timeout
        while time.monotonic() < deadline:
            if not self.cover_executor._threads or all(
                not t.is_alive() for t in self.cover_executor._threads
            ):
                break
            time.sleep(0.2)
        else:
            self.logger.warning(
                f"[BotOrchestrator] Cover executor non chiuso entro {self._cover_shutdown_timeout}s, "
                f"procedo comunque."
            )

    def _cleanup_temp_dir(self) -> None:
        if not os.path.isdir(self.temp_dir):
            return
        try:
            if not os.listdir(self.temp_dir):
                os.rmdir(self.temp_dir)
            else:
                self.logger.debug("La cartella temp non è vuota, eliminazione ignorata.")
        except Exception as exc:
            self.logger.warning(f"Impossibile eliminare la cartella temporanea vuota: {exc}")

    def _log_missing_summary(self) -> None:
        miss_log = os.path.join(os.getcwd(), "itunes_miss.log")
        self.logger.info(
            f"[BotOrchestrator] Riepilogo completato. "
            f"Controlla {miss_log} e i log WARNING per i brani non trovati."
        )