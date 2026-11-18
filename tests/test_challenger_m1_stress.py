"""Adversarial stress tests for Milestone 1: Models, Fixtures, and Packaging."""

from __future__ import annotations

import json
from pathlib import Path
import re
import unittest

from bot.models.enums import (
    DSO_PORTAL_URLS,
    GITHUB_MIRROR_FILENAMES,
    JOINT_STATUS_COLORS,
    JOINT_STATUS_LABELS,
    REGION_NAMES,
    REGION_SHORT_NAMES,
    SINGLE_SLOT_COLORS,
    YASNO_API_URLS,
    DSORegion,
    IntervalStatus,
    JointStatus,
    SlotStatus,
)
from bot.models.schedule import (
    DailySchedule,
    JointInterval,
    RegionalScheduleResponse,
    SlotConfig,
    SubgroupSchedule,
    TimelineInterval,
    TimelineSummary,
    TwoGroupTimelineResponse,
)


class TestMockFixturesAdversarial(unittest.TestCase):
    """Stress-test all 4 mock fixtures against domain models and structural invariants."""

    FIXTURE_DIR = Path(__file__).parent.parent / "bot" / "fixtures"

    FIXTURE_FILES = {
        "kyiv": "mock_kyiv.json",
        "kyiv-region": "mock_kyiv_region.json",
        "dnipro": "mock_dnipro.json",
        "odesa": "mock_odesa.json",
    }

    EXPECTED_REGIONS = {
        "kyiv": {"id": "kyiv", "name": "м. Київ (ДТЕК КЕМ)", "groups": ["GPV1.1", "GPV3.2"]},
        "kyiv-region": {"id": "kyiv-region", "name": "Київська область (ДТЕК КРЕМ)", "groups": ["GPV2.1"]},
        "dnipro": {"id": "dnipro", "name": "Дніпропетровська область (ДТЕК ДНЕМ)", "groups": ["GPV4.1"]},
        "odesa": {"id": "odesa", "name": "Одеська область (ДТЕК ОЕМ)", "groups": ["GPV5.2"]},
    }

    def test_all_fixture_files_exist_and_non_empty(self):
        """Verify each expected fixture file exists and has size > 1000 bytes."""
        for region, filename in self.FIXTURE_FILES.items():
            path = self.FIXTURE_DIR / filename
            self.assertTrue(path.exists(), f"Fixture file missing: {path}")
            self.assertGreater(path.stat().st_size, 1000, f"Fixture file too small: {path}")

    def test_fixture_validation_against_regional_schedule_response(self):
        """Validate each mock fixture using RegionalScheduleResponse.model_validate."""
        for region_key, filename in self.FIXTURE_FILES.items():
            path = self.FIXTURE_DIR / filename
            with open(path, "r", encoding="utf-8") as f:
                raw_data = json.load(f)

            resp = RegionalScheduleResponse.model_validate(raw_data)
            expected = self.EXPECTED_REGIONS[region_key]

            self.assertEqual(resp.region_id, expected["id"])
            self.assertEqual(resp.region_name, expected["name"])
            self.assertTrue(re.match(r"^\d{4}-\d{2}-\d{2}$", resp.date), f"Invalid date: {resp.date}")
            self.assertFalse(resp.is_emergency, "Mock fixture should default to normal mode")

            # Check group keys match expected
            self.assertEqual(set(resp.groups.keys()), set(expected["groups"]))

    def test_slot_count_integrity_and_continuity(self):
        """Verify every group in every fixture has strictly 48 slots spanning 00:00 to 24:00."""
        for filename in self.FIXTURE_FILES.values():
            path = self.FIXTURE_DIR / filename
            with open(path, "r", encoding="utf-8") as f:
                raw_data = json.load(f)

            resp = RegionalScheduleResponse.model_validate(raw_data)
            for group_key, group in resp.groups.items():
                self.assertEqual(
                    len(group.slots),
                    48,
                    f"Group {group_key} in {filename} must have exactly 48 slots, got {len(group.slots)}"
                )

                # Validate slot sequential indices and interval continuity
                for idx, slot in enumerate(group.slots):
                    self.assertEqual(slot.slot_index, idx, f"Slot index mismatch at position {idx} in {group_key}")
                    self.assertIsInstance(slot.status, IntervalStatus)
                    self.assertIn(slot.status, [IntervalStatus.ON, IntervalStatus.OFF, IntervalStatus.MAYBE, IntervalStatus.EMERGENCY])

                    # Verify time calculations
                    expected_start_min = idx * 30
                    expected_end_min = (idx + 1) * 30
                    expected_start = f"{expected_start_min // 60:02d}:{expected_start_min % 60:02d}"
                    expected_end = "24:00" if expected_end_min == 1440 else f"{expected_end_min // 60:02d}:{expected_end_min % 60:02d}"

                    self.assertEqual(slot.start_time, expected_start, f"Slot {idx} start_time mismatch in {group_key}")
                    self.assertEqual(slot.end_time, expected_end, f"Slot {idx} end_time mismatch in {group_key}")

                # First slot must start at 00:00, last slot must end at 24:00
                self.assertEqual(group.slots[0].start_time, "00:00")
                self.assertEqual(group.slots[47].end_time, "24:00")


class TestPydanticValidationBoundaries(unittest.TestCase):
    """Stress-test domain model boundary conditions and exception handling."""

    def test_timeline_interval_slot_index_boundaries(self):
        """Verify slot_index accepts 0..47 and strictly rejects <0 and >47."""
        # Valid boundaries
        slot_min = TimelineInterval(slot_index=0, start_time="00:00", end_time="00:30", status=IntervalStatus.ON)
        slot_max = TimelineInterval(slot_index=47, start_time="23:30", end_time="24:00", status=IntervalStatus.OFF)
        self.assertEqual(slot_min.slot_index, 0)
        self.assertEqual(slot_max.slot_index, 47)

        # Invalid boundaries
        with self.assertRaises((ValueError, Exception)):
            TimelineInterval(slot_index=-1, start_time="00:00", end_time="00:30", status=IntervalStatus.ON)

        with self.assertRaises((ValueError, Exception)):
            TimelineInterval(slot_index=48, start_time="24:00", end_time="24:30", status=IntervalStatus.ON)

        with self.assertRaises((ValueError, Exception)):
            TimelineInterval(slot_index=999, start_time="00:00", end_time="00:30", status=IntervalStatus.ON)

    def test_timeline_interval_invalid_status_rejection(self):
        """Verify invalid IntervalStatus strings are rejected."""
        with self.assertRaises((ValueError, Exception)):
            TimelineInterval(slot_index=0, start_time="00:00", end_time="00:30", status="INVALID_STATUS")

        with self.assertRaises((ValueError, Exception)):
            TimelineInterval(slot_index=0, start_time="00:00", end_time="00:30", status="")

        with self.assertRaises((ValueError, Exception)):
            TimelineInterval(slot_index=0, start_time="00:00", end_time="00:30", status="on")  # lowercase invalid

    def test_slot_config_boundaries(self):
        """Verify SlotConfig validates slot_number (1..2), group_id (1..6), and subgroup_id format."""
        # Valid config
        valid = SlotConfig(
            slot_number=1,
            alias="Home",
            region_code="kyiv",
            group_id=1,
            subgroup_id="1.1"
        )
        self.assertEqual(valid.slot_number, 1)
        self.assertEqual(valid.group_id, 1)

        # Invalid slot_number (<1 or >2)
        with self.assertRaises((ValueError, Exception)):
            SlotConfig(slot_number=0, alias="Home", region_code="kyiv", group_id=1, subgroup_id="1.1")

        with self.assertRaises((ValueError, Exception)):
            SlotConfig(slot_number=3, alias="Home", region_code="kyiv", group_id=1, subgroup_id="1.1")

        # Invalid group_id (<1 or >6)
        with self.assertRaises((ValueError, Exception)):
            SlotConfig(slot_number=1, alias="Home", region_code="kyiv", group_id=0, subgroup_id="1.1")

        with self.assertRaises((ValueError, Exception)):
            SlotConfig(slot_number=1, alias="Home", region_code="kyiv", group_id=7, subgroup_id="1.1")

        # Invalid subgroup_id format
        with self.assertRaises((ValueError, Exception)):
            SlotConfig(slot_number=1, alias="Home", region_code="kyiv", group_id=1, subgroup_id="invalid")

        with self.assertRaises((ValueError, Exception)):
            SlotConfig(slot_number=1, alias="Home", region_code="kyiv", group_id=1, subgroup_id="1.3")

    def test_subgroup_schedule_slot_count_validation(self):
        """Verify SubgroupSchedule requires exactly 48 slots and rejects !=48."""
        valid_slots = [
            TimelineInterval(
                slot_index=i,
                start_time=f"{i//2:02d}:{30*(i%2):02d}",
                end_time="24:00" if i == 47 else f"{(i+1)//2:02d}:{30*((i+1)%2):02d}",
                status=IntervalStatus.ON
            )
            for i in range(48)
        ]

        # Valid 48-slot schedule
        sched = SubgroupSchedule(group_id=2, subgroup_id="2.1", slots=valid_slots)
        self.assertEqual(len(sched.slots), 48)

        # Invalid group_id in SubgroupSchedule (<1 or >6)
        with self.assertRaises((ValueError, Exception)):
            SubgroupSchedule(group_id=0, subgroup_id="1.1", slots=valid_slots)

        with self.assertRaises((ValueError, Exception)):
            SubgroupSchedule(group_id=7, subgroup_id="7.1", slots=valid_slots)

        # Underflow: 47 slots
        with self.assertRaises((ValueError, Exception)):
            SubgroupSchedule(group_id=2, subgroup_id="2.1", slots=valid_slots[:47])

        # Overflow: 49 slots
        extra_slot = TimelineInterval(slot_index=0, start_time="00:00", end_time="00:30", status=IntervalStatus.ON)
        with self.assertRaises((ValueError, Exception)):
            SubgroupSchedule(group_id=2, subgroup_id="2.1", slots=valid_slots + [extra_slot])

        # Empty slots
        with self.assertRaises((ValueError, Exception)):
            SubgroupSchedule(group_id=2, subgroup_id="2.1", slots=[])

    def test_daily_schedule_and_timeline_response_length_checks(self):
        """Verify DailySchedule and TwoGroupTimelineResponse enforce exactly 48 items."""
        valid_intervals = [IntervalStatus.ON] * 48

        daily = DailySchedule(
            region=DSORegion.KYIV,
            group="GPV1.1",
            date="2026-11-11",
            intervals=valid_intervals
        )
        self.assertEqual(len(daily.intervals), 48)

        # Reject != 48 intervals in DailySchedule
        with self.assertRaises((ValueError, Exception)):
            DailySchedule(
                region=DSORegion.KYIV,
                group="GPV1.1",
                date="2026-11-11",
                intervals=valid_intervals[:47]
            )

        with self.assertRaises((ValueError, Exception)):
            DailySchedule(
                region=DSORegion.KYIV,
                group="GPV1.1",
                date="2026-11-11",
                intervals=valid_intervals + [IntervalStatus.OFF]
            )

    def test_two_group_timeline_response_validation(self):
        """Verify TwoGroupTimelineResponse requires slot_a, summary, and 48 timeline items."""
        slot_a = SlotConfig(slot_number=1, alias="Home", region_code="kyiv", group_id=1, subgroup_id="1.1")
        summary = TimelineSummary(current_joint_status=JointStatus.BOTH_ON)
        valid_timeline = [
            JointInterval(
                slot_index=i,
                start_time=f"{i//2:02d}:{30*(i%2):02d}",
                end_time="24:00" if i == 47 else f"{(i+1)//2:02d}:{30*((i+1)%2):02d}",
                status_a=IntervalStatus.ON,
                status_b=IntervalStatus.OFF,
                joint_status=JointStatus.GROUP1_ONLY
            )
            for i in range(48)
        ]

        resp = TwoGroupTimelineResponse(
            comparison_date="2026-11-11",
            slot_a=slot_a,
            summary=summary,
            timeline=valid_timeline
        )
        self.assertEqual(len(resp.timeline), 48)

        # Underflow in timeline
        with self.assertRaises((ValueError, Exception)):
            TwoGroupTimelineResponse(
                comparison_date="2026-11-11",
                slot_a=slot_a,
                summary=summary,
                timeline=valid_timeline[:47]
            )


class TestEnumAndMetadataCoverage(unittest.TestCase):
    """Stress-test coverage of all enums, UI colors, and Ukrainian labels."""

    EXPECTED_JOINT_STATUS_SPEC = {
        JointStatus.BOTH_ON: {"color": "#22C55E", "label": "Світло всюди"},
        JointStatus.GROUP1_ONLY: {"color": "#3B82F6", "label": "Світло лише у {slot1}"},
        JointStatus.GROUP2_ONLY: {"color": "#8B5CF6", "label": "Світло лише у {slot2}"},
        JointStatus.BOTH_OFF: {"color": "#1F2937", "label": "Світла немає ніде"},
        JointStatus.GROUP1_ON_GROUP2_MAYBE: {"color": "#059669", "label": "{slot1} Є, {slot2} Можливо"},
        JointStatus.GROUP1_MAYBE_GROUP2_ON: {"color": "#7C3AED", "label": "{slot1} Можливо, {slot2} Є"},
        JointStatus.GROUP1_OFF_GROUP2_MAYBE: {"color": "#4B5563", "label": "{slot1} Немає, {slot2} Можливо"},
        JointStatus.GROUP1_MAYBE_GROUP2_OFF: {"color": "#374151", "label": "{slot1} Можливо, {slot2} Немає"},
        JointStatus.BOTH_MAYBE: {"color": "#EAB308", "label": "Можливе відключення всюди"},
        JointStatus.EMERGENCY: {"color": "#EF4444", "label": "Аварійні відключення"},
    }

    def test_joint_status_member_count(self):
        """Verify JointStatus contains exactly 10 enum members."""
        self.assertEqual(len(JointStatus), 10, "JointStatus must contain exactly 10 enum values")

    def test_joint_status_colors_and_labels_match_specification(self):
        """Verify every JointStatus has the exact color and Ukrainian label from PROMPT_SPECIFICATION.md."""
        for status, expected in self.EXPECTED_JOINT_STATUS_SPEC.items():
            self.assertIn(status, JOINT_STATUS_COLORS, f"Missing color for {status}")
            self.assertEqual(JOINT_STATUS_COLORS[status], expected["color"], f"Color mismatch for {status}")

            self.assertIn(status, JOINT_STATUS_LABELS, f"Missing label for {status}")
            self.assertEqual(JOINT_STATUS_LABELS[status], expected["label"], f"Label mismatch for {status}")

    def test_single_slot_colors_match_specification(self):
        """Verify single slot colors match specification."""
        expected_colors = {
            IntervalStatus.ON: "#22C55E",
            IntervalStatus.OFF: "#1F2937",
            IntervalStatus.MAYBE: "#EAB308",
            IntervalStatus.EMERGENCY: "#EF4444",
        }
        for status, color in expected_colors.items():
            self.assertIn(status, SINGLE_SLOT_COLORS)
            self.assertEqual(SINGLE_SLOT_COLORS[status], color)

    def test_dso_region_metadata_coverage(self):
        """Verify all 4 DSO regions have names, URLs, and mirror filenames."""
        for region in DSORegion:
            self.assertIn(region, REGION_NAMES)
            self.assertIn(region, REGION_SHORT_NAMES)
            self.assertIn(region, DSO_PORTAL_URLS)
            self.assertTrue(DSO_PORTAL_URLS[region].startswith("https://"))
            self.assertIn(region, GITHUB_MIRROR_FILENAMES)
            self.assertTrue(GITHUB_MIRROR_FILENAMES[region].endswith(".json"))

        # YASNO API specifically active only for Kyiv and Dnipro
        self.assertIsNotNone(YASNO_API_URLS[DSORegion.KYIV])
        self.assertIsNotNone(YASNO_API_URLS[DSORegion.DNIPRO])
        self.assertIsNone(YASNO_API_URLS[DSORegion.KYIV_REGION])
        self.assertIsNone(YASNO_API_URLS[DSORegion.ODESA])


class TestModelSerializationRoundtrip(unittest.TestCase):
    """Stress-test serialization and deserialization round-trip integrity."""

    def test_regional_schedule_response_roundtrip(self):
        fixture_path = Path(__file__).parent.parent / "bot" / "fixtures" / "mock_kyiv.json"
        with open(fixture_path, "r", encoding="utf-8") as f:
            original_dict = json.load(f)

        resp1 = RegionalScheduleResponse.model_validate(original_dict)
        dumped = resp1.model_dump()
        resp2 = RegionalScheduleResponse.model_validate(dumped)

        self.assertEqual(resp1.region_id, resp2.region_id)
        self.assertEqual(resp1.date, resp2.date)
        self.assertEqual(len(resp1.groups), len(resp2.groups))
        for group_id in resp1.groups:
            self.assertEqual(len(resp1.groups[group_id].slots), len(resp2.groups[group_id].slots))


class TestPackagingAndDependencies(unittest.TestCase):
    """Verify requirements.txt and pyproject.toml compliance."""

    PROJECT_ROOT = Path(__file__).parent.parent

    def test_requirements_txt_contains_all_core_dependencies(self):
        req_path = self.PROJECT_ROOT / "requirements.txt"
        self.assertTrue(req_path.exists())
        content = req_path.read_text(encoding="utf-8")

        expected_pkgs = [
            "aiogram>=3.15.0",
            "aiosqlite>=0.20.0",
            "pydantic>=2.7.0",
            "pydantic-settings>=2.0.0",
            "pillow>=10.0.0",
            "curl_cffi>=0.7.0",
            "httpx>=0.27.0",
            "orjson>=3.10.0",
            "pytest>=8.0.0",
            "pytest-asyncio>=0.23.0",
        ]
        for pkg in expected_pkgs:
            self.assertIn(pkg, content)

    def test_pyproject_toml_configuration(self):
        pyproject_path = self.PROJECT_ROOT / "pyproject.toml"
        self.assertTrue(pyproject_path.exists())
        content = pyproject_path.read_text(encoding="utf-8")

        self.assertIn('name = "dtek_bot"', content)
        self.assertIn('requires-python = ">=3.11"', content)
        self.assertIn('asyncio_mode = "auto"', content)
        self.assertIn('testpaths = ["tests"]', content)


if __name__ == "__main__":
    unittest.main()
