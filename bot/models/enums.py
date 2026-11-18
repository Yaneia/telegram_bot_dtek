"""Domain enumerations and regional metadata constants."""

from enum import Enum
from typing import Dict, Optional


class IntervalStatus(str, Enum):
    """Core 30-minute interval availability status."""

    ON = "ON"
    OFF = "OFF"
    MAYBE = "MAYBE"
    EMERGENCY = "EMERGENCY"


# Specification compatibility alias
SlotStatus = IntervalStatus


class JointStatus(str, Enum):
    """Joint availability status from 3x3 combinatorial matrix plus emergency mode."""

    BOTH_ON = "BOTH_ON"
    GROUP1_ONLY = "GROUP1_ONLY"
    GROUP2_ONLY = "GROUP2_ONLY"
    BOTH_OFF = "BOTH_OFF"
    GROUP1_ON_GROUP2_MAYBE = "GROUP1_ON_GROUP2_MAYBE"
    GROUP1_MAYBE_GROUP2_ON = "GROUP1_MAYBE_GROUP2_ON"
    GROUP1_OFF_GROUP2_MAYBE = "GROUP1_OFF_GROUP2_MAYBE"
    GROUP1_MAYBE_GROUP2_OFF = "GROUP1_MAYBE_GROUP2_OFF"
    BOTH_MAYBE = "BOTH_MAYBE"
    EMERGENCY = "EMERGENCY"


# Specification compatibility alias
JointTimelineStatus = JointStatus


class DSORegion(str, Enum):
    """Supported DTEK Distribution System Operators."""

    KYIV = "kyiv"
    KYIV_REGION = "kyiv-region"
    DNIPRO = "dnipro"
    ODESA = "odesa"


# Authoritative UI color codes for rendering Lane 3 and badges
JOINT_STATUS_COLORS: Dict[JointStatus, str] = {
    JointStatus.BOTH_ON: "#22C55E",
    JointStatus.GROUP1_ONLY: "#3B82F6",
    JointStatus.GROUP2_ONLY: "#8B5CF6",
    JointStatus.BOTH_OFF: "#1F2937",
    JointStatus.GROUP1_ON_GROUP2_MAYBE: "#059669",
    JointStatus.GROUP1_MAYBE_GROUP2_ON: "#7C3AED",
    JointStatus.GROUP1_OFF_GROUP2_MAYBE: "#4B5563",
    JointStatus.GROUP1_MAYBE_GROUP2_OFF: "#374151",
    JointStatus.BOTH_MAYBE: "#EAB308",
    JointStatus.EMERGENCY: "#EF4444",
}

# Authoritative Ukrainian labels for timeline display
JOINT_STATUS_LABELS: Dict[JointStatus, str] = {
    JointStatus.BOTH_ON: "Світло всюди",
    JointStatus.GROUP1_ONLY: "Світло лише у {slot1}",
    JointStatus.GROUP2_ONLY: "Світло лише у {slot2}",
    JointStatus.BOTH_OFF: "Світла немає ніде",
    JointStatus.GROUP1_ON_GROUP2_MAYBE: "{slot1} Є, {slot2} Можливо",
    JointStatus.GROUP1_MAYBE_GROUP2_ON: "{slot1} Можливо, {slot2} Є",
    JointStatus.GROUP1_OFF_GROUP2_MAYBE: "{slot1} Немає, {slot2} Можливо",
    JointStatus.GROUP1_MAYBE_GROUP2_OFF: "{slot1} Можливо, {slot2} Немає",
    JointStatus.BOTH_MAYBE: "Можливе відключення всюди",
    JointStatus.EMERGENCY: "Аварійні відключення",
}

# Hex colors for single-group mode
SINGLE_SLOT_COLORS: Dict[IntervalStatus, str] = {
    IntervalStatus.ON: "#22C55E",
    IntervalStatus.OFF: "#1F2937",
    IntervalStatus.MAYBE: "#EAB308",
    IntervalStatus.EMERGENCY: "#EF4444",
}

# Full regional names matching official DTEK branding
REGION_NAMES: Dict[DSORegion, str] = {
    DSORegion.KYIV: "м. Київ (ДТЕК КЕМ)",
    DSORegion.KYIV_REGION: "Київська область (ДТЕК КРЕМ)",
    DSORegion.DNIPRO: "Дніпропетровська область (ДТЕК ДНЕМ)",
    DSORegion.ODESA: "Одеська область (ДТЕК ОЕМ)",
}

# Compact regional names for monospace tables and card pills
REGION_SHORT_NAMES: Dict[DSORegion, str] = {
    DSORegion.KYIV: "м. Київ",
    DSORegion.KYIV_REGION: "Київська обл.",
    DSORegion.DNIPRO: "Дніпропетровська обл.",
    DSORegion.ODESA: "Одеська обл.",
}

# DSO official portal shutdown schedule pages
DSO_PORTAL_URLS: Dict[DSORegion, str] = {
    DSORegion.KYIV: "https://www.dtek-kem.com.ua/ua/shutdowns",
    DSORegion.KYIV_REGION: "https://www.dtek-krem.com.ua/ua/shutdowns",
    DSORegion.DNIPRO: "https://www.dtek-dnem.com.ua/ua/shutdowns",
    DSORegion.ODESA: "https://www.dtek-oem.com.ua/ua/shutdowns",
}

# YASNO secondary API endpoints (Kyiv and Dnipro only)
YASNO_API_URLS: Dict[DSORegion, Optional[str]] = {
    DSORegion.KYIV: "https://api.yasno.com.ua/api/v1/pages/home/schedule-turn-off-electricity",
    DSORegion.DNIPRO: "https://api.yasno.com.ua/api/v1/pages/home/schedule-turn-off-electricity-dnipro",
    DSORegion.KYIV_REGION: None,
    DSORegion.ODESA: None,
}

# GitHub community mirror file names
GITHUB_MIRROR_FILENAMES: Dict[DSORegion, str] = {
    DSORegion.KYIV: "kem.json",
    DSORegion.KYIV_REGION: "krem.json",
    DSORegion.DNIPRO: "dnem.json",
    DSORegion.ODESA: "oem.json",
}
