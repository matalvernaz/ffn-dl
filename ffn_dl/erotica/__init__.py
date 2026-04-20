"""Erotica-focused scrapers.

Kept in a subpackage — instead of alongside the general-purpose site
modules at ``ffn_dl/*`` — so the erotica surface is a single visible
bucket in both the file tree and the import graph. Each module inside
follows the same interface as any other ``BaseScraper`` subclass and
is wired into :mod:`ffn_dl.sites` the same way.

Listing one site per module (not a monolithic ``erotica.py``) matches
the existing convention — ``ao3.py``, ``literotica.py``, ``royalroad.py``
— and keeps per-site selectors/docs readable.

Sites covered: AFF (Adult-FanFiction.org), StoriesOnline (SOL), Nifty,
SexStories (xnxx), MCStories, Lushstories, and Fictionmania. The
unified Erotic Story Search window (:mod:`ffn_dl.gui_search`) fans out
across all of them.

Sites considered and not included in this release:

* ASSTR — domain offline; no DNS resolution.
* Kristen Archives — JS fingerprint gate that curl_cffi can't bypass
  without a browser runtime.
* BDSM Library — connection times out (site unreachable).
* BigCloset TopShelf, Dark Wanderer — structurally different (Drupal
  and XenForo forum respectively) and left for a follow-up.
"""

from .aff import AFFScraper
from .fictionmania import FictionmaniaScraper
from .literotica import LiteroticaScraper
from .lushstories import LushStoriesScraper
from .mcstories import MCStoriesScraper
from .nifty import NiftyScraper
from .sexstories import SexStoriesScraper
from .storiesonline import StoriesOnlineScraper

__all__ = [
    "AFFScraper",
    "FictionmaniaScraper",
    "LiteroticaScraper",
    "LushStoriesScraper",
    "MCStoriesScraper",
    "NiftyScraper",
    "SexStoriesScraper",
    "StoriesOnlineScraper",
]
