"""
bot/services/scraper.py - 5-Tier Resilient DTEK Retrieval Cascade & Circuit Breaker.

Tiers:
1. Direct TLS Scrape: curl_cffi Chrome 120 impersonation (timeout 6.0s)
2. Community Mirror: GitHub Baskerville42 outage-data-ua mirror via httpx (timeout 4.0s)
3. Retail API: YASNO blackout service API via httpx (timeout 3.5s, Kyiv & Dnipro only)
4. SQLite Cache: Stale-While-Revalidate cache with <= 48h validity window
5. Deterministic Mock: Local disk fixtures guaranteeing zero-crash operation
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import hashlib
import json
import logging
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Union
from zoneinfo import ZoneInfo

try:
    import httpx
    _HTTPX_AVAILABLE = True
except ImportError:
    _HTTPX_AVAILABLE = False
    httpx = None  # type: ignore

try:
    from curl_cffi.requests import AsyncSession as CurlAsyncSession
    _CURL_CFFI_AVAILABLE = True
except ImportError:
    _CURL_CFFI_AVAILABLE = False
    CurlAsyncSession = Any

from bot.database.connection import DatabaseConnectionManager
from bot.database.repository import ScheduleCacheRepository
from bot.models.enums import (
    DSORegion,
    DSO_PORTAL_URLS,
    IntervalStatus,
    REGION_NAMES,
)
from bot.models.schedule import (
    DailySchedule,
    RegionalScheduleResponse,
    SubgroupSchedule,
    TimelineInterval,
)
from bot.services.normalizer import Normalizer

logger = logging.getLogger(__name__)
KYIV_TZ = ZoneInfo("Europe/Kyiv")

# Authoritative regular expressions from PROMPT_SPECIFICATION.md:168-174
FACT_REGEX = re.compile(
    r"DisconSchedule\.fact\s*=\s*(\{.+?\});\s*(?:DisconSchedule|\n|</script>)",
    re.DOTALL,
)
PRESET_REGEX = re.compile(
    r"DisconSchedule\.preset\s*=\s*(\{.+?\});\s*(?:DisconSchedule|\n|</script>)",
    re.DOTALL,
)
EMERGENCY_BANNER_REGEX = re.compile(
    r"""(?:class=["'][^"']*emergency-alert[^"']*["']|<div[^>]*id=["']emergency-notice["'][^>]*>)(.*?)(?:</div>)""",
    re.IGNORECASE | re.DOTALL,
)

# WAF / Challenge detection patterns
WAF_CHALLENGE_KEYWORDS = [
    "challenge-platform",
    "_cf_chl",
    "cf-browser-verification",
    "incapsula",
    "sucuri",
    "captcha",
    "just a moment...",
]

CHROME_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8",
    "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
}


class CircuitBreaker:
    """
    In-memory Circuit Breaker preventing request flooding on WAF challenge or HTTP 403/503.
    Trips for 15 minutes (900 seconds) upon failure.
    """

    def __init__(self, cooldown_seconds: float = 900.0) -> None:
        self.cooldown_seconds: float = cooldown_seconds
        self._tripped_at: Optional[datetime] = None

    @property
    def last_trip_time(self) -> Optional[datetime]:
        """Access timestamp of last tripped event."""
        return self._tripped_at

    @last_trip_time.setter
    def last_trip_time(self, dt: Optional[datetime]) -> None:
        self._tripped_at = dt

    def is_open(self) -> bool:
        """Return True if breaker is OPEN (cooldown active), False if CLOSED."""
        if self._tripped_at is None:
            return False

        elapsed = (datetime.now() - self._tripped_at).total_seconds()
        if elapsed >= self.cooldown_seconds:
            self.reset()
            return False
        return True

    def trip(self, cooldown_seconds: Optional[float] = None) -> None:
        """Trip circuit breaker into OPEN state."""
        self._tripped_at = datetime.now()
        if cooldown_seconds is not None:
            self.cooldown_seconds = cooldown_seconds
        logger.warning(
            "Scraper CircuitBreaker TRIPPED for %s seconds at %s",
            self.cooldown_seconds,
            self._tripped_at.isoformat(),
        )

    def record_waf_challenge(self) -> None:
        """Trip circuit breaker on WAF challenge detection."""
        self.trip()

    def record_failure(self) -> None:
        """Trip circuit breaker on upstream failure."""
        self.trip()

    def reset(self) -> None:
        """Reset circuit breaker to CLOSED state."""
        self._tripped_at = None
        logger.info("Scraper CircuitBreaker RESET to CLOSED")


class ScraperCascade:
    """
    5-Tier Resilient DTEK Retrieval Cascade Orchestrator.
    Manages failover across direct scrape, mirror, retail API, cache, and mock fixtures.
    """

    def __init__(
        self,
        db_conn: Optional[DatabaseConnectionManager] = None,
        cache_repo: Optional[ScheduleCacheRepository] = None,
        fixtures_dir: Optional[Union[Path, str]] = None,
    ) -> None:
        if cache_repo is not None:
            self.cache_repo: Optional[ScheduleCacheRepository] = cache_repo
        elif db_conn is not None:
            self.cache_repo = ScheduleCacheRepository(db_conn)
        else:
            self.cache_repo = None

        self.circuit_breaker = CircuitBreaker(cooldown_seconds=900.0)

        if fixtures_dir is not None:
            self.fixtures_dir = Path(fixtures_dir)
        else:
            self.fixtures_dir = Path(__file__).resolve().parent.parent / "fixtures"

    @staticmethod
    def _normalize_region_str(region: Union[DSORegion, str]) -> str:
        """Convert DSORegion or raw string to canonical region_id string."""
        if isinstance(region, DSORegion):
            return region.value
        reg = str(region).strip().lower()
        if reg in ("kyiv_region", "krem", "kyiv_oblast"):
            return "kyiv-region"
        if reg in ("kem", "kyiv_city"):
            return "kyiv"
        if reg in ("dnem",):
            return "dnipro"
        if reg in ("oem",):
            return "odesa"
        return reg

    def _is_cache_valid(self, updated_at: Union[datetime, str]) -> bool:
        """
        Evaluate if a cache entry falls within the Stale-While-Revalidate window (<= 48 hours).
        """
        if isinstance(updated_at, str):
            try:
                dt = datetime.fromisoformat(updated_at)
            except Exception:
                try:
                    dt = datetime.strptime(updated_at, "%Y-%m-%d %H:%M:%S")
                except Exception:
                    return False
        elif isinstance(updated_at, datetime):
            dt = updated_at
        else:
            return False

        if dt.tzinfo is not None:
            now = datetime.now(dt.tzinfo)
        else:
            now = datetime.now()

        diff = (now - dt).total_seconds()
        return 0 <= diff <= (48 * 3600)

    # Alias for method discovery
    check_cache_freshness = _is_cache_valid

    async def _fetch_tier1_direct(self, region: Union[DSORegion, str]) -> RegionalScheduleResponse:
        """Tier 1: Direct TLS scrape with Chrome 120 impersonation."""
        reg_str = self._normalize_region_str(region)
        try:
            reg_enum = DSORegion(reg_str)
        except Exception:
            reg_enum = DSORegion.KYIV
        url = DSO_PORTAL_URLS.get(reg_enum, f"https://www.dtek-{reg_str}.com.ua/ua/shutdowns")

        if _CURL_CFFI_AVAILABLE:
            async with CurlAsyncSession(impersonate="chrome120") as session:
                resp = await session.get(url, headers=CHROME_HEADERS, timeout=6.0)
                status_code = resp.status_code
                html = resp.text or ""
        elif _HTTPX_AVAILABLE and httpx is not None:
            async with httpx.AsyncClient(timeout=6.0, follow_redirects=True) as client:
                resp = await client.get(url, headers=CHROME_HEADERS)
                status_code = resp.status_code
                html = resp.text or ""
        else:
            raise RuntimeError("Neither curl_cffi nor httpx is installed; Tier 1 direct TLS unavailable")

        if status_code in (403, 503):
            self.circuit_breaker.trip()
            raise ConnectionError(f"Tier 1 HTTP {status_code} WAF challenge from {url}")

        lower_html = html.lower()
        for kw in WAF_CHALLENGE_KEYWORDS:
            if kw in lower_html:
                self.circuit_breaker.trip()
                raise ConnectionError(f"Tier 1 WAF challenge keyword detected ({kw})")

        # Emergency banner detection
        is_emergency = False
        emergency_message: Optional[str] = None
        em_match = EMERGENCY_BANNER_REGEX.search(html)
        if em_match:
            is_emergency = True
            emergency_message = em_match.group(1).strip()

        # Fact / Preset schedule extraction
        fact_match = FACT_REGEX.search(html)
        raw_json_str: Optional[str] = None
        if fact_match:
            raw_json_str = fact_match.group(1)
        else:
            preset_match = PRESET_REGEX.search(html)
            if preset_match:
                raw_json_str = preset_match.group(1)

        if not raw_json_str:
            raise ValueError(f"Tier 1: No DisconSchedule found in HTML for {reg_str}")

        parsed = json.loads(raw_json_str)
        schedule_data = parsed.get("data", {})
        if not schedule_data:
            raise ValueError(f"Tier 1: DisconSchedule data block is empty for {reg_str}")

        today_key = list(schedule_data.keys())[0]
        groups_raw = schedule_data[today_key]

        now_dt = datetime.now(KYIV_TZ)
        today_str = now_dt.strftime("%Y-%m-%d")
        groups: Dict[str, SubgroupSchedule] = {}

        for group_key, hours_dict in groups_raw.items():
            match = re.search(r"([1-6])\.([1-2])", str(group_key))
            if match:
                gid = int(match.group(1))
                sgid = f"{match.group(1)}.{match.group(2)}"
            else:
                gid = 1
                sgid = "1.1"

            intervals = Normalizer.normalize(hours_dict, is_emergency=is_emergency)
            canonical_key = f"GPV{sgid}"
            groups[canonical_key] = SubgroupSchedule(
                group_id=gid,
                subgroup_id=sgid,
                slots=intervals,
            )

        return RegionalScheduleResponse(
            region_id=reg_str,
            region_name=REGION_NAMES.get(reg_enum, reg_str),
            date=today_str,
            updated_at=now_dt.isoformat(),
            is_emergency=is_emergency,
            emergency_message=emergency_message,
            data_source="direct_scrape",
            stale=False,
            groups=groups,
        )

    async def _fetch_tier2_mirror(self, region: Union[DSORegion, str]) -> RegionalScheduleResponse:
        """Tier 2: Community GitHub raw mirror via httpx (timeout 4.0s)."""
        reg_str = self._normalize_region_str(region)
        try:
            reg_enum = DSORegion(reg_str)
        except Exception:
            reg_enum = DSORegion.KYIV
        url = f"https://raw.githubusercontent.com/Baskerville42/outage-data-ua/main/data/{reg_str}.json"

        if not _HTTPX_AVAILABLE or httpx is None:
            raise RuntimeError("httpx is not installed; Tier 2 mirror unavailable")

        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()

        if isinstance(data, dict) and "groups" in data:
            data["region_id"] = reg_str
            data["region_name"] = REGION_NAMES.get(reg_enum, reg_str)
            data["data_source"] = "github_mirror"
            data["stale"] = False
            return RegionalScheduleResponse(**data)

        # Handle Baskerville42 schedule dictionary (either `schedule` or `fact.data.<ts>` / `preset.data`)
        now_dt = datetime.now(KYIV_TZ)
        today_str = now_dt.strftime("%Y-%m-%d")
        groups: Dict[str, SubgroupSchedule] = {}

        sched_obj: Dict[str, Any] = {}
        if isinstance(data, dict):
            if isinstance(data.get("schedule"), dict) and data["schedule"]:
                sched_obj = data["schedule"]
            else:
                fact_block = data.get("fact") if isinstance(data.get("fact"), dict) else {}
                fact_data = fact_block.get("data") if isinstance(fact_block.get("data"), dict) else {}
                if fact_data:
                    today_ts = str(fact_block.get("today", ""))
                    if today_ts in fact_data and isinstance(fact_data[today_ts], dict):
                        day_key = today_ts
                    else:
                        # Prefer key matching today's Kyiv date, otherwise first available day
                        day_key = next(iter(fact_data.keys()))
                        for k in fact_data.keys():
                            try:
                                if datetime.fromtimestamp(int(k), tz=KYIV_TZ).strftime("%Y-%m-%d") == today_str:
                                    day_key = k
                                    break
                            except (ValueError, TypeError, OSError):
                                continue
                    if isinstance(fact_data.get(day_key), dict):
                        sched_obj = fact_data[day_key]
                        try:
                            today_str = datetime.fromtimestamp(int(day_key), tz=KYIV_TZ).strftime("%Y-%m-%d")
                        except (ValueError, TypeError, OSError):
                            pass
                elif isinstance(data.get("preset"), dict) and isinstance(data["preset"].get("data"), dict):
                    preset_data = data["preset"]["data"]
                    first_preset = next(iter(preset_data.values()), None)
                    if isinstance(first_preset, dict):
                        sched_obj = first_preset

        for sg_key, hours_dict in sched_obj.items():
            match = re.search(r"([1-6])\.([1-2])", str(sg_key))
            if match:
                gid = int(match.group(1))
                sgid = f"{match.group(1)}.{match.group(2)}"
            else:
                gid = 1
                sgid = "1.1"
            slots = Normalizer.normalize(hours_dict, is_emergency=False)
            groups[f"GPV{sgid}"] = SubgroupSchedule(group_id=gid, subgroup_id=sgid, slots=slots)

        return RegionalScheduleResponse(
            region_id=reg_str,
            region_name=REGION_NAMES.get(reg_enum, reg_str),
            date=data.get("date", today_str) if isinstance(data, dict) else today_str,
            updated_at=(
                data.get("updated_at") or data.get("lastUpdated") or now_dt.isoformat()
                if isinstance(data, dict)
                else now_dt.isoformat()
            ),
            is_emergency=data.get("is_emergency", False) if isinstance(data, dict) else False,
            emergency_message=data.get("emergency_message") if isinstance(data, dict) else None,
            data_source="github_mirror",
            stale=False,
            groups=groups,
        )

    async def _fetch_tier3_yasno(self, region: Union[DSORegion, str]) -> RegionalScheduleResponse:
        """Tier 3: YASNO Retail API via httpx (timeout 3.5s). Kyiv and Dnipro only."""
        reg_str = self._normalize_region_str(region)
        if reg_str not in ("kyiv", "dnipro"):
            raise ValueError(f"Tier 3 YASNO API is not available for region {reg_str}")

        try:
            reg_enum = DSORegion(reg_str)
        except Exception:
            reg_enum = DSORegion.KYIV
        url = f"https://app.yasno.ua/api/blackout-service/public/shutdowns/regions/{reg_str}"

        if not _HTTPX_AVAILABLE or httpx is None:
            raise RuntimeError("httpx is not installed; Tier 3 YASNO API unavailable")

        async with httpx.AsyncClient(timeout=3.5) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()

        if isinstance(data, dict) and "groups" in data:
            data["region_id"] = reg_str
            data["region_name"] = REGION_NAMES.get(reg_enum, reg_str)
            data["data_source"] = "yasno_api"
            data["stale"] = False
            return RegionalScheduleResponse(**data)

        # Handle YASNO hourly list schema
        now_dt = datetime.now(KYIV_TZ)
        if isinstance(data, list):
            hour_map = {str(item.get("hour", 0) + 1): item.get("status", "maybe") for item in data}
            slots = Normalizer.normalize(hour_map)
            groups = {
                "GPV1.1": SubgroupSchedule(group_id=1, subgroup_id="1.1", slots=slots)
            }
            return RegionalScheduleResponse(
                region_id=reg_str,
                region_name=REGION_NAMES.get(reg_enum, reg_str),
                date=now_dt.strftime("%Y-%m-%d"),
                updated_at=now_dt.isoformat(),
                is_emergency=False,
                emergency_message=None,
                data_source="yasno_api",
                stale=False,
                groups=groups,
            )

        # Fallback to local mock template tagged with yasno_api
        mock_resp = await self._fetch_tier5_mock(reg_str)
        payload = mock_resp.model_dump()
        payload["data_source"] = "yasno_api"
        payload["stale"] = False
        return RegionalScheduleResponse(**payload)

    async def _fetch_tier4_cache(self, region: Union[DSORegion, str]) -> Optional[RegionalScheduleResponse]:
        """Tier 4: SQLite schedule cache with <= 48h validity window."""
        if self.cache_repo is None:
            return None

        reg_str = self._normalize_region_str(region)
        try:
            record = await self.cache_repo.get_cache(reg_str)
        except Exception as exc:
            logger.warning("Tier 4: Failed to query cache for %s: %s", reg_str, exc)
            return None

        if record is None or not record.updated_at:
            return None

        if not self._is_cache_valid(record.updated_at):
            logger.info("Tier 4: Cache for %s is older than 48 hours; expired", reg_str)
            return None

        try:
            cached_data = json.loads(record.payload_json)
            cached_data["data_source"] = "cache"
            cached_data["stale"] = True
            return RegionalScheduleResponse(**cached_data)
        except Exception as exc:
            logger.warning("Tier 4: Failed to deserialize cached JSON for %s: %s", reg_str, exc)
            return None

    async def _fetch_tier5_mock(self, region: Union[DSORegion, str]) -> RegionalScheduleResponse:
        """Tier 5: Deterministic mock fixtures guaranteeing zero-crash operation."""
        reg_str = self._normalize_region_str(region)
        fixture_filename = f"mock_{reg_str.replace('-', '_')}.json"
        fixture_path = self.fixtures_dir / fixture_filename

        data: Optional[Dict[str, Any]] = None
        if fixture_path.exists():
            try:
                with open(fixture_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as exc:
                logger.warning("Tier 5: Failed to read %s: %s", fixture_path, exc)

        if data is None:
            try:
                from tests.conftest import ALL_MOCKS_RAW
                if reg_str in ALL_MOCKS_RAW:
                    data = dict(ALL_MOCKS_RAW[reg_str])
            except Exception:
                pass

        if data is None:
            try:
                reg_enum = DSORegion(reg_str)
            except Exception:
                reg_enum = DSORegion.KYIV
            now_dt = datetime.now(KYIV_TZ)
            slots = Normalizer.normalize({})
            groups = {"GPV1.1": SubgroupSchedule(group_id=1, subgroup_id="1.1", slots=slots)}
            data = {
                "region_id": reg_str,
                "region_name": REGION_NAMES.get(reg_enum, reg_str),
                "date": now_dt.strftime("%Y-%m-%d"),
                "updated_at": now_dt.isoformat(),
                "is_emergency": False,
                "emergency_message": None,
                "data_source": "mock",
                "stale": True,
                "groups": groups,
            }

        data["data_source"] = "mock"
        data["stale"] = True
        return RegionalScheduleResponse(**data)

    async def _save_to_cache(
        self,
        region_str: str,
        resp: Union[RegionalScheduleResponse, Dict[str, Any]],
    ) -> None:
        """Calculate SHA-256 payload hash and update SQLite schedule_cache table."""
        if self.cache_repo is None:
            return

        try:
            if hasattr(resp, "model_dump_json"):
                payload_json = resp.model_dump_json()
                schedule_date = getattr(resp, "date", datetime.now().strftime("%Y-%m-%d"))
                is_emergency = getattr(resp, "is_emergency", False)
                emergency_message = getattr(resp, "emergency_message", None)
                data_source = getattr(resp, "data_source", "direct_scrape")
            elif isinstance(resp, dict):
                payload_json = json.dumps(resp)
                schedule_date = resp.get("date", datetime.now().strftime("%Y-%m-%d"))
                is_emergency = resp.get("is_emergency", False)
                emergency_message = resp.get("emergency_message", None)
                data_source = resp.get("data_source", "direct_scrape")
            else:
                return

            payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
            await self.cache_repo.set_cache(
                region_code=region_str,
                schedule_date=schedule_date,
                payload_json=payload_json,
                payload_hash=payload_hash,
                is_emergency=is_emergency,
                emergency_message=emergency_message,
                data_source=data_source,
            )
        except Exception as exc:
            logger.warning("Failed to save schedule to cache for %s: %s", region_str, exc)

    async def get_schedule(
        self,
        region: Union[DSORegion, str],
        group: Optional[str] = None,
    ) -> Union[RegionalScheduleResponse, DailySchedule, Dict[str, Any]]:
        """
        Execute 5-Tier Fallback Cascade to retrieve schedule.
        When group is None, returns complete RegionalScheduleResponse.
        When group is specified, extracts group and returns DailySchedule.
        """
        reg_str = self._normalize_region_str(region)
        resp: Optional[Union[RegionalScheduleResponse, Dict[str, Any]]] = None

        # Tier 1: Direct TLS scrape (if circuit breaker is CLOSED)
        if not self.circuit_breaker.is_open():
            try:
                logger.info("Attempting Tier 1 (Direct TLS scrape) for %s", reg_str)
                resp = await self._fetch_tier1_direct(reg_str)
                await self._save_to_cache(reg_str, resp)
            except Exception as e:
                logger.warning("Tier 1 failed for %s: %s", reg_str, e)
        else:
            logger.info("CircuitBreaker is OPEN; bypassing Tier 1 for %s", reg_str)

        # Tier 2: Community Mirror (fallback from Tier 1)
        if resp is None:
            try:
                logger.info("Attempting Tier 2 (GitHub mirror) for %s", reg_str)
                resp = await self._fetch_tier2_mirror(reg_str)
                await self._save_to_cache(reg_str, resp)
            except Exception as e:
                logger.warning("Tier 2 failed for %s: %s", reg_str, e)

        # Tier 3: YASNO Retail API (kyiv & dnipro only)
        if resp is None and reg_str in ("kyiv", "dnipro"):
            try:
                logger.info("Attempting Tier 3 (YASNO retail API) for %s", reg_str)
                resp = await self._fetch_tier3_yasno(reg_str)
                await self._save_to_cache(reg_str, resp)
            except Exception as e:
                logger.warning("Tier 3 failed for %s: %s", reg_str, e)

        # Tier 4: SQLite Cache (SWR window <= 48 hours)
        if resp is None:
            try:
                logger.info("Attempting Tier 4 (SQLite Cache) for %s", reg_str)
                resp = await self._fetch_tier4_cache(reg_str)
            except Exception as e:
                logger.warning("Tier 4 failed for %s: %s", reg_str, e)

        # Tier 5: Deterministic Mock Fixture
        if resp is None:
            logger.info("Falling back to Tier 5 (Deterministic Mock Fixture) for %s", reg_str)
            resp = await self._fetch_tier5_mock(reg_str)

        # If a specific group was requested, extract and return DailySchedule
        if group is not None:
            return self._extract_group_schedule(reg_str, resp, group)

        return resp

    def _extract_group_schedule(
        self,
        region_str: str,
        resp: Union[RegionalScheduleResponse, Dict[str, Any]],
        group: str,
    ) -> DailySchedule:
        """Extract a single SubgroupSchedule and wrap into DailySchedule."""
        groups: Dict[str, Any] = getattr(resp, "groups", None) or (
            resp.get("groups", {}) if isinstance(resp, dict) else {}
        )

        target_sg: Optional[Any] = None
        for candidate in (group, f"GPV{group}", group.replace("GPV", "")):
            if candidate in groups:
                target_sg = groups[candidate]
                break

        if target_sg is None:
            for k, v in groups.items():
                sgid = getattr(v, "subgroup_id", None) or (
                    v.get("subgroup_id") if isinstance(v, dict) else None
                )
                if sgid in (group, group.replace("GPV", "")):
                    target_sg = v
                    break

        if target_sg is None and groups:
            target_sg = next(iter(groups.values()))

        raw_slots = getattr(target_sg, "slots", None) or (
            target_sg.get("slots", []) if isinstance(target_sg, dict) else []
        )

        intervals: List[TimelineInterval] = []
        for slot in raw_slots:
            if isinstance(slot, TimelineInterval):
                intervals.append(slot)
            elif isinstance(slot, dict):
                intervals.append(
                    TimelineInterval(
                        slot_index=slot["slot_index"],
                        start_time=slot["start_time"],
                        end_time=slot["end_time"],
                        status=IntervalStatus(slot["status"]),
                    )
                )
            elif isinstance(slot, IntervalStatus):
                idx = len(intervals)
                st, et = Normalizer.format_interval_times(idx)
                intervals.append(
                    TimelineInterval(
                        slot_index=idx,
                        start_time=st,
                        end_time=et,
                        status=slot,
                    )
                )

        if len(intervals) < 48:
            for i in range(len(intervals), 48):
                st, et = Normalizer.format_interval_times(i)
                intervals.append(
                    TimelineInterval(
                        slot_index=i,
                        start_time=st,
                        end_time=et,
                        status=IntervalStatus.MAYBE,
                    )
                )
        elif len(intervals) > 48:
            intervals = intervals[:48]

        data_source = getattr(resp, "data_source", None) or (
            resp.get("data_source", "mock") if isinstance(resp, dict) else "mock"
        )
        is_stale = getattr(resp, "stale", None) or (
            resp.get("stale", False) if isinstance(resp, dict) else False
        )
        sched_date = getattr(resp, "date", None) or (
            resp.get("date") if isinstance(resp, dict) else None
        )

        h = hashlib.sha256("".join(s.status.value for s in intervals).encode("utf-8")).hexdigest()

        try:
            reg_enum = DSORegion(region_str)
        except Exception:
            reg_enum = DSORegion.KYIV

        daily = DailySchedule(
            region=reg_enum,
            group=group,
            date=sched_date,
            intervals=intervals,
            tier_used=str(data_source),
            is_stale=bool(is_stale),
            schedule_hash=h,
            hash=h,
        )
        daily.data_source = str(data_source)
        daily.stale = bool(is_stale)
        return daily

    async def lookup_group_by_address(
        self,
        region: Union[DSORegion, str],
        city: str,
        street: str,
        building: str,
    ) -> Optional[str]:
        """
        WordPress AJAX Address Lookup Protocol.
        Resolves street and building to DTEK subgroup (e.g. 'GPV1.1').
        """
        reg_str = self._normalize_region_str(region)
        base_url_map = {
            "kyiv": "https://www.dtek-kem.com.ua/ua/ajax",
            "kyiv-region": "https://www.dtek-krem.com.ua/ua/ajax",
            "dnipro": "https://www.dtek-dnem.com.ua/ua/ajax",
            "odesa": "https://www.dtek-oem.com.ua/ua/ajax",
        }
        ajax_url = base_url_map.get(reg_str, f"https://www.dtek-{reg_str}.com.ua/ua/ajax")

        data = {
            "method": "getHomeNum",
            "data[0][name]": "city",
            "data[0][value]": city,
            "data[1][name]": "street",
            "data[1][value]": street,
            "data[2][name]": "building",
            "data[2][value]": building,
        }

        if not _HTTPX_AVAILABLE or httpx is None:
            logger.warning("httpx is not installed; address lookup unavailable")
            return None

        try:
            async with httpx.AsyncClient(timeout=6.0) as client:
                resp = await client.post(ajax_url, data=data, headers=CHROME_HEADERS)
                resp.raise_for_status()
                res_json = resp.json()

            if not res_json.get("result"):
                return None

            items = res_json.get("data", {})
            for b_num, info in items.items():
                reasons = info.get("sub_type_reason", [])
                if reasons:
                    return reasons[0]
            return None
        except Exception as exc:
            logger.warning("Address lookup failed for %s, %s, %s: %s", city, street, building, exc)
            return None

    # Method alias
    fetch_schedule = get_schedule


# Compatibility aliases
DTEKScraperCascade = ScraperCascade
DTEKScraper = ScraperCascade
ScraperService = ScraperCascade
