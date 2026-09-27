"""Every auction site gets one adapter that yields RawLot objects.
Adding a new site = writing one new subclass. Nothing else changes."""
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

log = logging.getLogger(__name__)


@dataclass
class RawLot:
    source: str
    external_id: str
    url: str
    title: str
    current_price: float
    description: str = ""
    image_url: str = ""
    bid_count: int | None = None
    end_time: datetime | None = None
    raw: dict = field(default_factory=dict)


class SourceBlocked(Exception):
    """The site refused us (403/429). We stop - we do NOT retry around blocks."""


class BaseAdapter:
    source = "base"
    request_delay_seconds = 3.0   # be polite: one request every few seconds
    max_pages_per_keyword = 2

    def search(self, keyword: str):
        """Yield RawLot objects for one keyword."""
        raise NotImplementedError

    def pause(self):
        time.sleep(self.request_delay_seconds)
