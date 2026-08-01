from __future__ import annotations

import logging
from typing import Optional

from Model.Song import Song
from Pipeline.PipelineResult import MBResult
from Providers.MusicBrainzApi import MusicBrainzApiRequstor


class YearResolver:
   
    def __init__(self, mb: MusicBrainzApiRequstor, safe_call, logger: Optional[logging.Logger] = None) -> None:
        self.mb = mb
        self._safe_call = safe_call
        self.log = logger or logging.getLogger(__name__)

    async def fix_bad_year(self, song: Song, mb: MBResult) -> None:
        year = str(song.meta.year).strip()

        if mb.found and mb.recording:
            for date_src in (
                mb.recording.get("first-release-date", ""),
                (mb.album or {}).get("date", ""),
                (mb.album or {}).get("first-release-date", ""),
            ):
                if date_src and len(date_src) >= 4 and date_src[:4].isdigit():
                    mb_year = int(date_src[:4])
                    current = int(year) if (year and year.isdigit()) else 9999
                    if 1900 <= mb_year <= 2100 and mb_year < current:
                        song.meta.year = str(mb_year)
                        return
                    if 1900 <= mb_year <= 2100 and not (year and year.isdigit()):
                        song.meta.year = str(mb_year)
                        return

        if not year or not year.isdigit() or not (1900 <= int(year) <= 2100):
            song.meta.year = ""
            rec_id = (mb.recording or {}).get("id") if mb.found else None
            if not rec_id:
                return
            rec = await self._safe_call(self.mb.fetch_recording_by_id, "MB-year", rec_id, inc_params="releases+release-groups")
            if not rec:
                return
            for date_src in (
                rec.get("first-release-date", ""),
                (rec.get("release-groups") or [{}])[0].get("first-release-date", ""),
                (rec.get("releases") or [{}])[0].get("date", ""),
            ):
                if date_src and len(date_src) >= 4 and date_src[:4].isdigit():
                    y = int(date_src[:4])
                    if 1900 <= y <= 2100:
                        song.meta.year = str(y)
                        return