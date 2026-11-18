"""Dual-Slot Unified Comparison Engine.

Executes 3x3 combinatorial matrix evaluations, single-slot fallback,
daily summary metric calculations, Europe/Kyiv current slot and transition tracking,
and smart Ukrainian recommendation generation.
"""

from __future__ import annotations

from datetime import datetime, time
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union
from zoneinfo import ZoneInfo

from bot.models.enums import DSORegion, IntervalStatus, JointStatus, REGION_SHORT_NAMES
from bot.models.schedule import (
    JointInterval,
    SlotConfig,
    TimelineSummary,
    TwoGroupTimelineResponse,
)

KYIV_TZ = ZoneInfo("Europe/Kyiv")

# Authoritative 3x3 Combinatorial Decision Matrix
COMBINATORIAL_MATRIX: Dict[Tuple[str, str], Tuple[str, str, str]] = {
    ("ON", "ON"): ("BOTH_ON", "#22C55E", "Світло всюди"),
    ("ON", "OFF"): ("GROUP1_ONLY", "#3B82F6", "Світло лише у [Slot 1]"),
    ("OFF", "ON"): ("GROUP2_ONLY", "#8B5CF6", "Світло лише у [Slot 2]"),
    ("OFF", "OFF"): ("BOTH_OFF", "#1F2937", "Світла немає ніде"),
    ("ON", "MAYBE"): ("GROUP1_ON_GROUP2_MAYBE", "#059669", "[Slot 1] Є, [Slot 2] Можливо"),
    ("MAYBE", "ON"): ("GROUP1_MAYBE_GROUP2_ON", "#7C3AED", "[Slot 1] Можливо, [Slot 2] Є"),
    ("OFF", "MAYBE"): ("GROUP1_OFF_GROUP2_MAYBE", "#4B5563", "[Slot 1] Немає, [Slot 2] Можливо"),
    ("MAYBE", "OFF"): ("GROUP1_MAYBE_GROUP2_OFF", "#374151", "[Slot 1] Можливо, [Slot 2] Немає"),
    ("MAYBE", "MAYBE"): ("BOTH_MAYBE", "#EAB308", "Можливе відключення всюди"),
}

EXPECTED_COMBINATORIAL_MATRIX = COMBINATORIAL_MATRIX

# Authoritative color map supporting JointStatus enums, uppercase and lowercase string keys
COLOR_MAP: Dict[Union[JointStatus, str], str] = {
    "BOTH_ON": "#22C55E",
    "GROUP1_ONLY": "#3B82F6",
    "GROUP2_ONLY": "#8B5CF6",
    "BOTH_OFF": "#1F2937",
    "GROUP1_ON_GROUP2_MAYBE": "#059669",
    "GROUP1_MAYBE_GROUP2_ON": "#7C3AED",
    "GROUP1_OFF_GROUP2_MAYBE": "#4B5563",
    "GROUP1_MAYBE_GROUP2_OFF": "#374151",
    "BOTH_MAYBE": "#EAB308",
    "EMERGENCY": "#EF4444",
}
for _key, _val in list(COLOR_MAP.items()):
    COLOR_MAP[_key.lower()] = _val
    if hasattr(JointStatus, _key):
        COLOR_MAP[getattr(JointStatus, _key)] = _val

STATUS_COLORS = COLOR_MAP

# Authoritative Ukrainian label map supporting JointStatus enums, uppercase and lowercase keys
LABEL_MAP: Dict[Union[JointStatus, str], str] = {
    "BOTH_ON": "Світло всюди",
    "GROUP1_ONLY": "Світло лише",
    "GROUP2_ONLY": "Світло лише",
    "BOTH_OFF": "Світла немає ніде",
    "GROUP1_ON_GROUP2_MAYBE": "Можливо",
    "GROUP1_MAYBE_GROUP2_ON": "Можливо",
    "GROUP1_OFF_GROUP2_MAYBE": "Можливо",
    "GROUP1_MAYBE_GROUP2_OFF": "Можливо",
    "BOTH_MAYBE": "Можливе відключення всюди",
    "EMERGENCY": "Аварійні відключення",
}
for _key, _val in list(LABEL_MAP.items()):
    LABEL_MAP[_key.lower()] = _val
    if hasattr(JointStatus, _key):
        LABEL_MAP[getattr(JointStatus, _key)] = _val

STATUS_LABELS = LABEL_MAP


def _normalize_status_str(val: Any) -> str:
    """Extract uppercase status string from enum, dict, object, or string."""
    if val is None:
        return "MAYBE"
    if hasattr(val, "status"):
        val = getattr(val, "status")
    if hasattr(val, "value"):
        val = val.value
    elif hasattr(val, "name"):
        val = val.name
    elif isinstance(val, dict):
        val = val.get("status") or val.get("value") or "MAYBE"
        if hasattr(val, "status"):
            val = getattr(val, "status")
        if hasattr(val, "value"):
            val = val.value
        elif hasattr(val, "name"):
            val = val.name
    return str(val).strip().upper()


def _to_interval_status(val: Any) -> IntervalStatus:
    """Safely convert any value to IntervalStatus without raising ValueError."""
    if isinstance(val, IntervalStatus):
        return val
    norm_str = _normalize_status_str(val)
    try:
        return IntervalStatus(norm_str)
    except (ValueError, KeyError):
        return IntervalStatus.MAYBE


def evaluate_pair(s1: Any, s2: Any = None) -> JointStatus:
    """
    Evaluate joint status for a single 30-minute interval from Slot 1 and Slot 2 states.
    Strictly follows 3x3 matrix and emergency override rules.
    """
    str1 = _normalize_status_str(s1)
    if s2 is None:
        if str1 == "EMERGENCY":
            return JointStatus.EMERGENCY
        if str1 == "ON":
            return JointStatus.BOTH_ON
        if str1 == "OFF":
            return JointStatus.BOTH_OFF
        return JointStatus.BOTH_MAYBE

    str2 = _normalize_status_str(s2)

    # Ukrenergo emergency override: if either slot is EMERGENCY, joint is EMERGENCY
    if str1 == "EMERGENCY" or str2 == "EMERGENCY":
        return JointStatus.EMERGENCY

    matrix_entry = COMBINATORIAL_MATRIX.get((str1, str2))
    if matrix_entry:
        return JointStatus(matrix_entry[0])

    return JointStatus.BOTH_MAYBE


get_joint_status = evaluate_pair
evaluate_joint_status = evaluate_pair
resolve_joint_status = evaluate_pair


def get_current_slot_index(t: Optional[Any] = None) -> int:
    """
    Compute 0..47 interval index for Kyiv time: (hour * 60 + minute) // 30.
    """
    if t is None:
        now = datetime.now(KYIV_TZ)
        return min(47, max(0, (now.hour * 60 + now.minute) // 30))

    if isinstance(t, datetime):
        if t.tzinfo is not None:
            t_kyiv = t.astimezone(KYIV_TZ)
        else:
            t_kyiv = t
        return min(47, max(0, (t_kyiv.hour * 60 + t_kyiv.minute) // 30))

    if isinstance(t, time):
        return min(47, max(0, (t.hour * 60 + t.minute) // 30))

    if hasattr(t, "hour") and hasattr(t, "minute"):
        return min(47, max(0, (t.hour * 60 + t.minute) // 30))

    return 0


slot_index_for_time = get_current_slot_index


def _format_time_interval(index: int) -> Tuple[str, str]:
    """Format start and end time strings for slot index 0..47."""
    start_min = index * 30
    end_min = (index + 1) * 30
    sh, sm = divmod(start_min, 60)
    eh, em = divmod(end_min, 60)
    end_str = "24:00" if end_min == 1440 else f"{eh:02d}:{em:02d}"
    return f"{sh:02d}:{sm:02d}", end_str


def calculate_next_transition(
    timeline: Sequence[Any],
    current_slot_index: Optional[int] = None,
    now: Optional[datetime] = None,
) -> Tuple[str, int]:
    """
    Calculate next transition time ('HH:MM') and minutes countdown from current time.
    """
    if now is None:
        now_dt = datetime.now(KYIV_TZ)
    elif now.tzinfo is not None:
        now_dt = now.astimezone(KYIV_TZ)
    else:
        now_dt = now.replace(tzinfo=KYIV_TZ)

    if current_slot_index is None:
        cur_idx = get_current_slot_index(now_dt)
    else:
        cur_idx = current_slot_index

    if not timeline or cur_idx >= len(timeline):
        return "24:00", 0

    current_item = timeline[cur_idx]
    current_status = getattr(current_item, "joint_status", None) or (
        current_item.get("joint_status") if isinstance(current_item, dict) else current_item
    )
    current_status_str = _normalize_status_str(current_status)

    next_trans_time = "24:00"
    minutes_to_next = max(0, 1440 - (now_dt.hour * 60 + now_dt.minute))

    for idx in range(cur_idx + 1, len(timeline)):
        item = timeline[idx]
        item_status = getattr(item, "joint_status", None) or (
            item.get("joint_status") if isinstance(item, dict) else item
        )
        if _normalize_status_str(item_status) != current_status_str:
            item_start = getattr(item, "start_time", None) or (
                item.get("start_time") if isinstance(item, dict) else None
            )
            next_trans_time = item_start or _format_time_interval(idx)[0]
            next_minute = idx * 30
            cur_minute = now_dt.hour * 60 + now_dt.minute
            minutes_to_next = max(0, next_minute - cur_minute)
            break

    return next_trans_time, minutes_to_next


find_next_transition = calculate_next_transition


def _generate_recommendation(
    current_joint: JointStatus,
    alias_a: str = "Дім",
    alias_b: str = "Офіс",
    is_emergency: bool = False,
) -> str:
    """Generate concise factual status summary text in Ukrainian."""
    if is_emergency or current_joint == JointStatus.EMERGENCY:
        return "Аварійні відключення за розпорядженням Укренерго. Графіки не діють."
    if current_joint == JointStatus.BOTH_ON:
        return "Світло є в обох локаціях."
    if current_joint == JointStatus.GROUP1_ONLY:
        return f"Світло є лише у {alias_a}."
    if current_joint == JointStatus.GROUP2_ONLY:
        return f"Світло є лише у {alias_b}."
    if current_joint == JointStatus.BOTH_OFF:
        return "Світла немає в обох локаціях."
    if current_joint == JointStatus.GROUP1_ON_GROUP2_MAYBE:
        return f"У {alias_a} світло є, у {alias_b} можливі відключення."
    if current_joint == JointStatus.GROUP1_MAYBE_GROUP2_ON:
        return f"У {alias_b} світло є, у {alias_a} можливі відключення."
    return "Можливе відключення електроенергії."


def _parse_group_subgroup(
    meta: Dict[str, Any],
    default_g: int,
    default_sg: str,
) -> Tuple[int, str]:
    """Parse and validate group_id (1..6) and subgroup_id ('X.Y') from metadata."""
    raw_grp = meta.get("subgroup_id") or meta.get("group")
    gid = meta.get("group_id")
    sgid = meta.get("subgroup_id")

    if raw_grp:
        match = re.search(r"([1-6])\.([1-2])", str(raw_grp))
        if match:
            g_val = int(match.group(1))
            sg_val = f"{match.group(1)}.{match.group(2)}"
            return g_val, sg_val

    if isinstance(gid, int) and 1 <= gid <= 6:
        if sgid and re.match(r"^[1-6]\.[1-2]$", str(sgid)):
            return gid, str(sgid)
        return gid, f"{gid}.1"

    return default_g, default_sg


def _extract_slots_list(payload: Any) -> Tuple[List[str], Dict[str, Any]]:
    """
    Polymorphically extract 48 status strings and metadata from various payload types:
    SubgroupSchedule, DailySchedule, dict, list of strings/objects, or single string.
    """
    metadata: Dict[str, Any] = {}
    if payload is None:
        return ["MAYBE"] * 48, metadata

    # Check for dictionary
    if isinstance(payload, dict):
        metadata["alias"] = payload.get("alias")
        metadata["group_id"] = payload.get("group_id")
        metadata["subgroup_id"] = payload.get("subgroup_id") or payload.get("group")
        metadata["region"] = payload.get("region") or payload.get("region_code")
        metadata["region_name"] = payload.get("region_name")
        if payload.get("date"):
            metadata["date"] = payload.get("date")
        if payload.get("updated_at"):
            metadata["updated_at"] = payload.get("updated_at")
        if payload.get("is_stale") is not None or payload.get("stale") is not None:
            metadata["stale"] = bool(payload.get("is_stale") or payload.get("stale"))
        raw_items = payload.get("slots") or payload.get("intervals")
        if raw_items is not None:
            payload = raw_items

    # Check for domain model instances
    if hasattr(payload, "slots") and not isinstance(payload, dict):
        metadata["group_id"] = getattr(payload, "group_id", None)
        metadata["subgroup_id"] = getattr(payload, "subgroup_id", None)
        payload = getattr(payload, "slots")
    elif hasattr(payload, "intervals") and not isinstance(payload, dict):
        metadata["group"] = getattr(payload, "group", None)
        metadata["region"] = getattr(payload, "region", None)
        if getattr(payload, "date", None):
            metadata["date"] = getattr(payload, "date")
        if getattr(payload, "updated_at", None):
            metadata["updated_at"] = getattr(payload, "updated_at")
        if getattr(payload, "is_stale", None) is not None:
            metadata["stale"] = getattr(payload, "is_stale")
        payload = getattr(payload, "intervals")
    elif hasattr(payload, "groups") and not isinstance(payload, dict):
        metadata["region"] = getattr(payload, "region_id", None)
        metadata["region_name"] = getattr(payload, "region_name", None)
        if getattr(payload, "date", None):
            metadata["date"] = getattr(payload, "date")
        if getattr(payload, "updated_at", None):
            metadata["updated_at"] = getattr(payload, "updated_at")
        if getattr(payload, "stale", None) is not None:
            metadata["stale"] = getattr(payload, "stale")
        groups = getattr(payload, "groups", {})
        if isinstance(groups, dict) and groups:
            first_key = sorted(groups.keys())[0]
            first_sub = groups[first_key]
            metadata["subgroup_id"] = first_key
            metadata["group_id"] = getattr(first_sub, "group_id", None)
            payload = getattr(first_sub, "slots", [])

    # If payload has group, parse group_id and subgroup_id
    raw_grp = metadata.get("group") or metadata.get("subgroup_id")
    if raw_grp:
        match = re.search(r"([1-6])\.([1-2])", str(raw_grp))
        if match:
            if metadata.get("group_id") is None:
                metadata["group_id"] = int(match.group(1))
            if not metadata.get("subgroup_id") or not re.match(r"^[1-6]\.[1-2]$", str(metadata.get("subgroup_id"))):
                metadata["subgroup_id"] = f"{match.group(1)}.{match.group(2)}"

    # If payload is a single string or enum, replicate 48 times
    if isinstance(payload, (str, IntervalStatus)):
        status_val = _to_interval_status(payload).value
        return [status_val] * 48, metadata

    # If payload is a list/sequence
    if isinstance(payload, (list, tuple)):
        extracted: List[str] = []
        for item in payload:
            extracted.append(_to_interval_status(item).value)
        # Ensure exactly 48 items
        if len(extracted) < 48:
            extracted = extracted + ["MAYBE"] * (48 - len(extracted))
        elif len(extracted) > 48:
            extracted = extracted[:48]
        return extracted, metadata

    return ["MAYBE"] * 48, metadata


class ComparisonJointInterval(JointInterval):
    """JointInterval subclass supporting dictionary subscript and get operations."""

    def __getitem__(self, item: str) -> Any:
        if hasattr(self, item):
            return getattr(self, item)
        raise KeyError(item)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def __contains__(self, key: str) -> bool:
        return hasattr(self, key)


class ComparisonTimelineSummary(TimelineSummary):
    """TimelineSummary subclass supporting dictionary subscript and get operations."""

    def __getitem__(self, item: str) -> Any:
        if hasattr(self, item):
            return getattr(self, item)
        raise KeyError(item)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def __contains__(self, key: str) -> bool:
        return hasattr(self, key)


class ComparisonSlotConfig(SlotConfig):
    """SlotConfig subclass supporting dictionary subscript and get operations."""

    def __getitem__(self, item: str) -> Any:
        if hasattr(self, item):
            return getattr(self, item)
        raise KeyError(item)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def __contains__(self, key: str) -> bool:
        return hasattr(self, key)


class TimelineResponse(TwoGroupTimelineResponse):
    """TwoGroupTimelineResponse subclass supporting dictionary subscript and get operations."""

    def __getitem__(self, item: str) -> Any:
        if hasattr(self, item):
            return getattr(self, item)
        raise KeyError(item)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def __contains__(self, key: str) -> bool:
        return hasattr(self, key)

    def keys(self) -> List[str]:
        return [
            "comparison_date", "is_emergency", "emergency_message",
            "stale", "slot_a", "slot_b", "summary", "timeline",
        ]


class ComparisonEngine:
    """
    Unified dual-slot timeline comparison engine.
    Computes 48-interval joint status, daily summary metrics, transitions,
    and Ukrainian recommendations.
    """

    COLOR_MAP = COLOR_MAP
    STATUS_COLORS = STATUS_COLORS
    LABEL_MAP = LABEL_MAP
    STATUS_LABELS = STATUS_LABELS
    EXPECTED_COMBINATORIAL_MATRIX = EXPECTED_COMBINATORIAL_MATRIX

    evaluate_pair = staticmethod(evaluate_pair)
    get_joint_status = staticmethod(get_joint_status)
    evaluate_joint_status = staticmethod(evaluate_joint_status)
    resolve_joint_status = staticmethod(resolve_joint_status)
    get_current_slot_index = staticmethod(get_current_slot_index)
    slot_index_for_time = staticmethod(slot_index_for_time)
    calculate_next_transition = staticmethod(calculate_next_transition)
    find_next_transition = staticmethod(find_next_transition)

    @classmethod
    def compare(
        cls,
        slot1: Any,
        slot2: Optional[Any] = None,
        now: Optional[datetime] = None,
        alias_a: Optional[str] = None,
        alias_b: Optional[str] = None,
        is_emergency: Optional[bool] = None,
        stale: bool = False,
        date: Optional[str] = None,
        **kwargs: Any,
    ) -> TimelineResponse:
        """
        Compare two schedule slots and produce a full unified timeline comparison response.
        Supports single-slot fallback mode when slot2 is None.
        """
        if now is None:
            now_dt = datetime.now(KYIV_TZ)
        elif now.tzinfo is not None:
            now_dt = now.astimezone(KYIV_TZ)
        else:
            now_dt = now.replace(tzinfo=KYIV_TZ)

        slots_a_str, meta_a = _extract_slots_list(slot1)
        single_mode = slot2 is None
        slots_b_str, meta_b = _extract_slots_list(slot2) if not single_mode else ([], {})

        # Defensive 48-slot length guarantee
        if len(slots_a_str) < 48:
            slots_a_str = slots_a_str + ["MAYBE"] * (48 - len(slots_a_str))
        elif len(slots_a_str) > 48:
            slots_a_str = slots_a_str[:48]

        if not single_mode:
            if len(slots_b_str) < 48:
                slots_b_str = slots_b_str + ["MAYBE"] * (48 - len(slots_b_str))
            elif len(slots_b_str) > 48:
                slots_b_str = slots_b_str[:48]

        # Resolve aliases
        final_alias_a = alias_a or meta_a.get("alias") or kwargs.get("alias_1") or "Дім"
        final_alias_b = alias_b or meta_b.get("alias") or kwargs.get("alias_2") or "Офіс"

        # Determine emergency state
        slot1_all_emerg = len(slots_a_str) == 48 and all(s == "EMERGENCY" for s in slots_a_str)
        slot2_all_emerg = (not single_mode) and len(slots_b_str) == 48 and all(s == "EMERGENCY" for s in slots_b_str)
        forced_emergency = (is_emergency is True) or slot1_all_emerg or slot2_all_emerg

        # Build intervals
        timeline_items: List[ComparisonJointInterval] = []
        both_on_cnt = 0
        g1_only_cnt = 0
        g2_only_cnt = 0
        both_off_cnt = 0
        uncertain_cnt = 0

        for i in range(48):
            start_t, end_t = _format_time_interval(i)
            sa_str = slots_a_str[i]
            sb_str = slots_b_str[i] if not single_mode else None

            if forced_emergency or sa_str == "EMERGENCY" or sb_str == "EMERGENCY":
                joint = JointStatus.EMERGENCY
                if single_mode:
                    sa_enum = IntervalStatus.EMERGENCY
                    sb_enum = None
                else:
                    sa_enum = IntervalStatus.EMERGENCY if sa_str == "EMERGENCY" else _to_interval_status(sa_str)
                    sb_enum = IntervalStatus.EMERGENCY if sb_str == "EMERGENCY" else _to_interval_status(sb_str)
            elif single_mode:
                sa_enum = _to_interval_status(sa_str)
                sb_enum = None
                if sa_enum == IntervalStatus.ON:
                    joint = JointStatus.BOTH_ON
                elif sa_enum == IntervalStatus.OFF:
                    joint = JointStatus.BOTH_OFF
                else:
                    joint = JointStatus.BOTH_MAYBE
            else:
                sa_enum = _to_interval_status(sa_str)
                sb_enum = _to_interval_status(sb_str)
                joint = evaluate_pair(sa_enum.value, sb_enum.value)

            # Metric counters
            if joint == JointStatus.BOTH_ON:
                both_on_cnt += 1
            elif joint == JointStatus.GROUP1_ONLY:
                g1_only_cnt += 1
            elif joint == JointStatus.GROUP2_ONLY:
                g2_only_cnt += 1
            elif joint == JointStatus.BOTH_OFF:
                both_off_cnt += 1

            if "MAYBE" in joint.value:
                uncertain_cnt += 1

            item = ComparisonJointInterval(
                slot_index=i,
                start_time=start_t,
                end_time=end_t,
                status_a=sa_enum,
                status_b=sb_enum,
                joint_status=joint,
            )
            timeline_items.append(item)

        # Slot index & transition
        cur_slot_idx = get_current_slot_index(now_dt)
        cur_joint = timeline_items[cur_slot_idx].joint_status
        next_trans_time, mins_to_next = calculate_next_transition(
            timeline_items, current_slot_index=cur_slot_idx, now=now_dt
        )

        # Ukrainian smart recommendation
        smart_rec = _generate_recommendation(
            current_joint=cur_joint,
            alias_a=final_alias_a,
            alias_b=final_alias_b,
            is_emergency=forced_emergency,
        )

        # Metrics
        summary = ComparisonTimelineSummary(
            both_on_hours=round(both_on_cnt * 0.5, 2),
            group1_only_hours=round(g1_only_cnt * 0.5, 2),
            group2_only_hours=round(0.0 if single_mode else g2_only_cnt * 0.5, 2),
            both_off_hours=round(both_off_cnt * 0.5, 2),
            uncertain_hours=round(uncertain_cnt * 0.5, 2),
            current_slot_index=cur_slot_idx,
            current_joint_status=cur_joint,
            next_transition_time=next_trans_time,
            minutes_to_next_transition=mins_to_next,
            smart_recommendation=smart_rec,
        )

        # Slot configs
        group_id_a, subgroup_id_a = _parse_group_subgroup(meta_a, default_g=1, default_sg="1.1")
        reg_code_a = meta_a.get("region") or DSORegion.KYIV
        reg_name_a = meta_a.get("region_name") or REGION_SHORT_NAMES.get(reg_code_a, "м. Київ")

        slot_a_cfg = ComparisonSlotConfig(
            slot_number=1,
            alias=final_alias_a,
            region_code=reg_code_a,
            region_name=reg_name_a,
            group_id=group_id_a,
            subgroup_id=subgroup_id_a,
        )

        slot_b_cfg = None
        if not single_mode:
            group_id_b, subgroup_id_b = _parse_group_subgroup(meta_b, default_g=2, default_sg="2.1")
            reg_code_b = meta_b.get("region") or DSORegion.KYIV
            reg_name_b = meta_b.get("region_name") or REGION_SHORT_NAMES.get(reg_code_b, "м. Київ")

            slot_b_cfg = ComparisonSlotConfig(
                slot_number=2,
                alias=final_alias_b,
                region_code=reg_code_b,
                region_name=reg_name_b,
                group_id=group_id_b,
                subgroup_id=subgroup_id_b,
            )

        comp_date = (
            date
            or meta_a.get("date")
            or (meta_b.get("date") if not single_mode else None)
            or now_dt.strftime("%Y-%m-%d")
        )
        is_stale_flag = (
            stale
            or bool(meta_a.get("stale"))
            or (bool(meta_b.get("stale")) if not single_mode else False)
        )

        raw_updated = meta_a.get("updated_at") or (meta_b.get("updated_at") if not single_mode else None)
        formatted_updated: Optional[str] = None
        if raw_updated:
            raw_u_str = str(raw_updated).strip()
            try:
                dt_u = datetime.fromisoformat(raw_u_str.replace("Z", "+00:00"))
                if dt_u.tzinfo is not None:
                    dt_u = dt_u.astimezone(KYIV_TZ)
                formatted_updated = dt_u.strftime("%d.%m.%Y %H:%M")
            except Exception:
                formatted_updated = raw_u_str

        return TimelineResponse(
            comparison_date=comp_date,
            updated_at=formatted_updated,
            is_emergency=forced_emergency,
            emergency_message="Аварійні відключення за розпорядженням Укренерго!" if forced_emergency else None,
            stale=is_stale_flag,
            slot_a=slot_a_cfg,
            slot_b=slot_b_cfg,
            summary=summary,
            timeline=timeline_items,
        )

    compute_timeline = compare


# Specification compatibility aliases
TimelineEngine = ComparisonEngine
UnifiedTimelineEngine = ComparisonEngine
