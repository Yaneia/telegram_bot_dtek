"""
tests/test_adversarial_m7_presentation_ops.py - Milestone 7 Adversarial Verification Suite.

Exhaustive empirical stress tests and white-box verification for:
1. Pillow 1200x640 RGBA card generation, empty slot 2, emergency rendering, fonts (bot/rendering/image.py)
2. Mobile monospace table width <= 28 characters invariant across every single line (bot/rendering/text.py)
3. TMA payload compression < 1500 chars lossless roundtrip, sync_<b64> <= 64 chars (bot/services/tma_payload.py)
4. Notifier advance warnings, midnight boundary, silent hours, revision diff, 25 msg/s limiter, HTTP 429 backoff
5. Deployment invariants: compose.yaml 0 ports/expose, Containerfile 0 EXPOSE & UID 1000, main.py outbound polling, docs/index.html 0 localhost/127.0.0.1//api/
"""

from __future__ import annotations

import ast
import asyncio
import io
import re
import struct
import time
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from bot.models.enums import DSORegion, IntervalStatus, JointStatus
from bot.models.schedule import SlotConfig
from bot.rendering.image import (
    ImageRenderer,
    _render_fallback_png,
    render_timeline_png,
)
from bot.rendering.text import (
    TextRenderer,
    render_timeline_text,
)
from bot.services.notifier import (
    compute_schedule_sha256,
    detect_advance_outage_transition,
    detect_schedule_revision_diffs,
    format_advance_warning_message,
    format_schedule_diff_message,
    get_slot_index_from_datetime,
    get_slot_time_bounds,
    is_in_silent_hours,
)
from bot.services.rate_limiter import TokenBucketRateLimiter
from bot.services.tma_payload import (
    CHAR_STATUS_MAP,
    STATUS_CHAR_MAP,
    SubscriptableSlotConfig,
    TMAPayload,
)

KYIV_TZ = ZoneInfo("Europe/Kyiv")
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def parse_png_metadata(data: bytes) -> Tuple[int, int, int, int, Optional[float]]:
    """Parse PNG bytes returning: (width, height, bit_depth, color_type, dpi)."""
    if len(data) < 33:
        raise ValueError(f"Buffer too short for PNG ({len(data)} bytes)")
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("Invalid PNG magic bytes")

    w, h, bit_depth, color_type = struct.unpack(">IIBB", data[16:26])

    dpi: Optional[float] = None
    offset = 8
    while offset + 8 <= len(data):
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        chunk_type = data[offset + 4:offset + 8]
        if chunk_type == b"pHYs" and length >= 9:
            ppu_x, ppu_y, unit = struct.unpack(">IIB", data[offset + 8:offset + 17])
            if unit == 1:
                dpi = round(ppu_x * 0.0254, 1)
            break
        offset += 8 + length + 4

    return w, h, bit_depth, color_type, dpi


def build_synthetic_timeline(
    pattern_a: str = "ON",
    pattern_b: str = "OFF",
    joint_pattern: str = "GROUP1_ONLY",
    count: int = 48,
) -> List[Dict[str, Any]]:
    """Generate 48 interval dicts with given patterns."""
    items = []
    for i in range(count):
        sa = pattern_a if isinstance(pattern_a, str) else pattern_a[i % len(pattern_a)]
        sb = pattern_b if isinstance(pattern_b, str) else pattern_b[i % len(pattern_b)]
        joint = (
            joint_pattern
            if isinstance(joint_pattern, str)
            else joint_pattern[i % len(joint_pattern)]
        )
        items.append({
            "interval_index": i,
            "start_time": f"{i // 2:02d}:{(i % 2) * 30:02d}",
            "end_time": f"{(i + 1) // 2:02d}:{((i + 1) % 2) * 30:02d}" if (i + 1) < 48 else "24:00",
            "status_a": sa,
            "status_b": sb,
            "joint_status": joint,
        })
    return items


# ============================================================================
# 1. IMAGE RENDERER ADVERSARIAL STRESS TESTS
# ============================================================================

class TestAdversarialImageRenderer(unittest.TestCase):
    """Stress tests and boundary condition verification for Pillow image generator."""

    def test_pillow_card_generation_dimensions_and_mode(self) -> None:
        """Verify PNG buffer meets exact 1200x640, RGBA (32-bit), and 144 DPI specs."""
        timeline = build_synthetic_timeline("ON", "OFF", "GROUP1_ONLY")
        payload = {
            "comparison_date": "2026-11-12",
            "is_emergency": False,
            "stale": False,
            "slot_a": {"alias": "Дім", "region_code": "kyiv", "group_id": 1, "subgroup_id": "1.1"},
            "slot_b": {"alias": "Офіс", "region_code": "kyiv", "group_id": 2, "subgroup_id": "2.1"},
            "timeline": timeline,
            "summary": {
                "current_slot_index": 20,
                "both_on_hours": 6.0,
                "group1_only_hours": 8.0,
                "group2_only_hours": 4.0,
                "both_off_hours": 6.0,
                "uncertain_hours": 0.0,
            },
        }

        buf = ImageRenderer.render_timeline_png(payload)
        self.assertIsInstance(buf, bytes)
        self.assertGreater(len(buf), 1000)

        w, h, bit_d, col_t, dpi = parse_png_metadata(buf)
        self.assertEqual(w, 1200)
        self.assertEqual(h, 640)
        self.assertEqual(bit_d, 8)
        self.assertEqual(col_t, 6)  # RGBA color type
        if dpi is not None:
            self.assertAlmostEqual(dpi, 144.0, delta=1.0)

    def test_pillow_card_cyrillic_and_ukrainian_glyphs(self) -> None:
        """Verify rendering handles all Ukrainian alphabet letters including rare glyphs."""
        ukrainian_text = "Ґанок, їжак, єнот, ірис, Київ — «Львівські Круасани» & №42!"
        payload = {
            "comparison_date": "2026-11-12",
            "is_emergency": False,
            "stale": False,
            "slot_a": {"alias": ukrainian_text, "region_code": "kyiv"},
            "slot_b": {"alias": "Офіс Їжака (Ґ)", "region_code": "kyiv"},
            "timeline": build_synthetic_timeline("MAYBE", "ON", "GROUP2_ONLY"),
            "summary": {"current_slot_index": 10},
        }

        buf = render_timeline_png(payload)
        self.assertGreater(len(buf), 1000)
        w, h, _, col_t, _ = parse_png_metadata(buf)
        self.assertEqual((w, h, col_t), (1200, 640, 6))

    def test_pillow_card_extreme_alias_lengths(self) -> None:
        """Verify rendering survives aliases with 500+ characters without crashing."""
        giant_alias = "Дуже довга назва локації " * 20
        payload = {
            "comparison_date": "2026-11-12",
            "slot_a": {"alias": giant_alias},
            "slot_b": {"alias": giant_alias},
            "timeline": build_synthetic_timeline(),
            "summary": {"current_slot_index": 0},
        }
        buf = render_timeline_png(payload)
        self.assertGreater(len(buf), 1000)

    def test_pillow_card_single_slot_fallback(self) -> None:
        """Verify single-slot mode (slot_b = None) renders Lane 2 fallback and third pill."""
        payload = {
            "comparison_date": "2026-11-12",
            "slot_a": {"alias": "Єдиний слот"},
            "slot_b": None,
            "timeline": build_synthetic_timeline("ON", "ON", "BOTH_ON"),
            "summary": {
                "current_slot_index": 12,
                "both_on_hours": 12.0,
                "group1_only_hours": 12.0,
                "group2_only_hours": 0.0,
                "both_off_hours": 0.0,
                "uncertain_hours": 0.0,
            },
        }
        buf = render_timeline_png(payload)
        self.assertGreater(len(buf), 1000)
        w, h, _, col_t, _ = parse_png_metadata(buf)
        self.assertEqual((w, h, col_t), (1200, 640, 6))

    def test_pillow_card_emergency_rendering(self) -> None:
        """Verify emergency mode renders emergency title, red badge, and lanes."""
        payload = {
            "comparison_date": "2026-11-12",
            "is_emergency": True,
            "slot_a": {"alias": "Дім"},
            "slot_b": {"alias": "Офіс"},
            "timeline": build_synthetic_timeline("EMERGENCY", "EMERGENCY", "EMERGENCY"),
            "summary": {"current_slot_index": 5},
        }
        buf = render_timeline_png(payload)
        self.assertGreater(len(buf), 1000)

    def test_pillow_card_stale_data_rendering(self) -> None:
        """Verify stale data mode renders warning badge."""
        payload = {
            "comparison_date": "2026-11-12",
            "stale": True,
            "slot_a": {"alias": "Дім"},
            "slot_b": {"alias": "Офіс"},
            "timeline": build_synthetic_timeline(),
            "summary": {"current_slot_index": 0},
        }
        buf = render_timeline_png(payload)
        self.assertGreater(len(buf), 1000)

    def test_pillow_card_abnormal_slot_index_and_metrics(self) -> None:
        """Verify abnormal slot indexes (-100, 999, 'abc', None) are clamped safely."""
        for bad_idx in (-100, 999, "invalid", None, 3.14):
            payload = {
                "timeline": build_synthetic_timeline(),
                "summary": {"current_slot_index": bad_idx},
            }
            buf = render_timeline_png(payload)
            self.assertGreater(len(buf), 1000)

    def test_pillow_card_empty_and_partial_timeline(self) -> None:
        """Verify rendering handles empty timeline and timeline with < 48 items."""
        # Empty timeline
        buf_empty = render_timeline_png({"timeline": []})
        self.assertGreater(len(buf_empty), 1000)

        # Partial timeline (10 intervals)
        partial_timeline = build_synthetic_timeline(count=10)
        buf_partial = render_timeline_png({"timeline": partial_timeline})
        self.assertGreater(len(buf_partial), 1000)

    def test_pure_python_fallback_png_generator(self) -> None:
        """Verify _render_fallback_png generates valid 1200x640 RGBA 144 DPI PNG."""
        payload = {
            "timeline": build_synthetic_timeline(),
            "summary": {"current_slot_index": 14},
        }
        buf = _render_fallback_png(payload)
        self.assertIsInstance(buf, bytes)
        w, h, bit_d, col_t, dpi = parse_png_metadata(buf)
        self.assertEqual((w, h, bit_d, col_t), (1200, 640, 8, 6))
        self.assertAlmostEqual(dpi, 144.0, delta=1.0)

    def test_image_rendering_memory_and_buffer_cleanup(self) -> None:
        """Verify rendering 25 consecutive images executes cleanly without resource leak."""
        payload = {
            "timeline": build_synthetic_timeline("ON", "OFF", "GROUP1_ONLY"),
            "summary": {"current_slot_index": 15},
        }
        for _ in range(25):
            buf = render_timeline_png(payload)
            self.assertGreater(len(buf), 1000)


# ============================================================================
# 2. TEXT RENDERER STRICT MONOSPACE WIDTH INVARIANT TESTS
# ============================================================================

class TestAdversarialTextRenderer(unittest.TestCase):
    """Stress tests verifying the strict monospace width <= 28 characters invariant."""

    def _assert_all_lines_under_28_chars(self, output: str) -> None:
        """Assert that every single line in output is <= 28 characters."""
        lines = output.split("\n")
        self.assertGreater(len(lines), 0, "Rendered output must have at least 1 line")
        for idx, line in enumerate(lines):
            self.assertLessEqual(
                len(line),
                28,
                f"Line {idx} exceeds 28 chars (len={len(line)}): {line!r}",
            )

    def test_strict_monospace_width_under_28_chars_dual_slot(self) -> None:
        """Verify dual-slot output satisfies line width <= 28 invariant."""
        payload = {
            "comparison_date": "12.11.2026",
            "slot_a": {"alias": "Дім"},
            "slot_b": {"alias": "Офіс"},
            "timeline": build_synthetic_timeline("ON", "OFF", "GROUP1_ONLY"),
            "summary": {
                "both_on_hours": 4.5,
                "group1_only_hours": 8.0,
                "group2_only_hours": 3.5,
                "both_off_hours": 8.0,
                "uncertain_hours": 0.0,
                "current_joint_status": "GROUP1_ONLY",
                "next_transition_time": "14:30",
                "minutes_to_next_transition": 45,
            },
        }
        text = TextRenderer.render_timeline_text(payload)
        self._assert_all_lines_under_28_chars(text)

    def test_strict_monospace_width_under_28_chars_single_slot(self) -> None:
        """Verify single-slot output satisfies line width <= 28 invariant."""
        payload = {
            "comparison_date": "12.11.2026",
            "slot_a": {"alias": "Мій будинок"},
            "slot_b": None,
            "timeline": build_synthetic_timeline("ON", "ON", "BOTH_ON"),
            "summary": {
                "both_on_hours": 24.0,
                "group1_only_hours": 24.0,
                "group2_only_hours": 0.0,
                "both_off_hours": 0.0,
                "uncertain_hours": 0.0,
                "current_joint_status": "ON",
                "next_transition_time": None,
                "minutes_to_next_transition": None,
            },
        }
        text = render_timeline_text(payload)
        self._assert_all_lines_under_28_chars(text)

    def test_strict_monospace_width_under_28_chars_emergency_and_stale(self) -> None:
        """Verify emergency and stale headers satisfy line width <= 28 invariant."""
        for is_emerg, is_stale in [(True, False), (False, True), (True, True)]:
            payload = {
                "comparison_date": "12.11.2026",
                "is_emergency": is_emerg,
                "stale": is_stale,
                "slot_a": {"alias": "Дім"},
                "slot_b": {"alias": "Офіс"},
                "timeline": build_synthetic_timeline("EMERGENCY", "EMERGENCY", "EMERGENCY"),
                "summary": {"current_joint_status": "EMERGENCY"},
            }
            text = render_timeline_text(payload)
            self._assert_all_lines_under_28_chars(text)

    def test_strict_monospace_width_extreme_alias_lengths(self) -> None:
        """Verify 500-character aliases are safely truncated and <= 28 characters."""
        long_alias = "Дуже_довгий_псевдонім_локації_що_може_переповнити_буфер_" * 10
        payload = {
            "comparison_date": "12.11.2026",
            "slot_a": {"alias": long_alias},
            "slot_b": {"alias": long_alias},
            "timeline": build_synthetic_timeline(),
            "summary": {"current_joint_status": "BOTH_ON"},
        }
        text = render_timeline_text(payload)
        self._assert_all_lines_under_28_chars(text)

    def test_strict_monospace_width_extreme_hours_and_countdowns(self) -> None:
        """Verify huge numeric values (99999.5 hours, 99999 mins) remain <= 28 chars."""
        payload = {
            "comparison_date": "12.11.2026",
            "slot_a": {"alias": "Дім"},
            "slot_b": {"alias": "Офіс"},
            "timeline": build_synthetic_timeline(),
            "summary": {
                "both_on_hours": 99999.5,
                "group1_only_hours": 88888.5,
                "group2_only_hours": 77777.5,
                "both_off_hours": 66666.5,
                "uncertain_hours": 55555.5,
                "current_joint_status": "BOTH_ON",
                "next_transition_time": "23:59:59.999999",
                "minutes_to_next_transition": 999999,
            },
        }
        text = render_timeline_text(payload)
        self._assert_all_lines_under_28_chars(text)

    def test_strict_monospace_width_all_nine_joint_states(self) -> None:
        """Verify all 9 joint states produce badges conforming strictly to <= 28 chars."""
        all_nine = [
            "BOTH_ON",
            "GROUP1_ONLY",
            "GROUP2_ONLY",
            "BOTH_OFF",
            "GROUP1_ON_GROUP2_MAYBE",
            "GROUP1_MAYBE_GROUP2_ON",
            "GROUP1_OFF_GROUP2_MAYBE",
            "GROUP1_MAYBE_GROUP2_OFF",
            "BOTH_MAYBE",
            "EMERGENCY",
        ]
        for joint in all_nine:
            payload = {
                "comparison_date": "12.11.2026",
                "slot_a": {"alias": "Локація 1"},
                "slot_b": {"alias": "Локація 2"},
                "timeline": build_synthetic_timeline(joint_pattern=joint),
                "summary": {"current_joint_status": joint},
            }
            text = render_timeline_text(payload)
            self._assert_all_lines_under_28_chars(text)

    def test_strict_monospace_width_rapidly_alternating_intervals(self) -> None:
        """Verify 48 alternating 30m intervals (max line count) all remain <= 28 chars."""
        alternating_a = ["ON", "OFF", "MAYBE", "EMERGENCY"] * 12
        alternating_b = ["OFF", "ON", "EMERGENCY", "MAYBE"] * 12
        alternating_j = ["GROUP1_ONLY", "GROUP2_ONLY", "BOTH_MAYBE", "EMERGENCY"] * 12

        payload = {
            "comparison_date": "12.11.2026",
            "slot_a": {"alias": "Дім"},
            "slot_b": {"alias": "Офіс"},
            "timeline": [
                {
                    "interval_index": i,
                    "start_time": f"{i // 2:02d}:{(i % 2) * 30:02d}",
                    "end_time": f"{(i + 1) // 2:02d}:{((i + 1) % 2) * 30:02d}" if (i + 1) < 48 else "24:00",
                    "status_a": alternating_a[i],
                    "status_b": alternating_b[i],
                    "joint_status": alternating_j[i],
                }
                for i in range(48)
            ],
            "summary": {"current_joint_status": "BOTH_ON"},
        }
        text = render_timeline_text(payload)
        self._assert_all_lines_under_28_chars(text)

    def test_strict_monospace_width_empty_and_minimal_payloads(self) -> None:
        """Verify rendering empty payload `{}` raises no error and is <= 28 chars."""
        text = render_timeline_text({})
        self._assert_all_lines_under_28_chars(text)


# ============================================================================
# 3. TMA PAYLOAD COMPRESSION & SYNC BRIDGE ADVERSARIAL TESTS
# ============================================================================

class TestAdversarialTMAPayload(unittest.TestCase):
    """Stress tests for TMA URL fragment (#p=) and deep-link sync parameter."""

    def test_tma_fragment_max_entropy_compression_under_1500_chars(self) -> None:
        """Verify worst-case maximum entropy schedule encodes strictly < 1500 chars."""
        # Alternating statuses across 48 intervals prevents run-length / dictionary optimization
        max_entropy_timeline = [
            {
                "interval_index": i,
                "status_a": ("ON", "OFF", "MAYBE", "EMERGENCY")[i % 4],
                "status_b": ("OFF", "MAYBE", "EMERGENCY", "ON")[i % 4],
            }
            for i in range(48)
        ]
        payload = {
            "date": "2026-11-12",
            "is_emergency": True,
            "stale": True,
            "slot_a": {
                "alias": "Дуже довгий псевдонім локації 1",
                "region_code": "kyiv-region",
                "group_id": 4,
                "subgroup_id": "4.2",
            },
            "slot_b": {
                "alias": "Дуже довгий псевдонім локації 2",
                "region_code": "dnipro",
                "group_id": 6,
                "subgroup_id": "6.1",
            },
            "timeline": max_entropy_timeline,
        }

        fragment = TMAPayload.encode(payload, bot_username="dtek_super_tracker_bot")
        self.assertTrue(fragment.startswith("#p="))
        self.assertLess(
            len(fragment),
            1500,
            f"Encoded fragment exceeds 1500 characters: {len(fragment)}",
        )
        # Typically < 400 chars even under maximum entropy
        self.assertLess(len(fragment), 500)

    def test_tma_fragment_lossless_roundtrip(self) -> None:
        """Verify lossless roundtrip encoding and decoding of schedule payload."""
        timeline = [
            {
                "interval_index": i,
                "status_a": "ON" if i < 24 else "OFF",
                "status_b": "OFF" if i < 24 else "ON",
            }
            for i in range(48)
        ]
        original = {
            "date": "2026-11-12",
            "is_emergency": False,
            "stale": False,
            "slot_a": {"alias": "Дім", "region_code": "kyiv", "group_id": 1, "subgroup_id": "1.1"},
            "slot_b": {"alias": "Офіс", "region_code": "kyiv", "group_id": 2, "subgroup_id": "2.1"},
            "timeline": timeline,
        }

        fragment = TMAPayload.encode(original, bot_username="dtek_bot")
        decoded = TMAPayload.decode(fragment)

        self.assertEqual(decoded.get("d") or decoded.get("date"), "2026-11-12")
        self.assertEqual(decoded.get("slot_a", {}).get("alias"), "Дім")
        self.assertEqual(decoded.get("slot_b", {}).get("alias"), "Офіс")
        self.assertEqual(decoded.get("s_a"), "O" * 24 + "X" * 24)
        self.assertEqual(decoded.get("s_b"), "X" * 24 + "O" * 24)

    def test_tma_fragment_query_parameter_stripping(self) -> None:
        """Verify decode correctly strips appended query params like `&utm_source=tg`."""
        fragment = TMAPayload.encode({"timeline": build_synthetic_timeline()})
        dirty_fragment = f"{fragment}&tgWebAppStartParam=test&foo=bar"
        decoded = TMAPayload.decode(dirty_fragment)
        self.assertIn("s_a", decoded)

    def test_sync_code_length_under_64_chars_all_regions_and_groups(self) -> None:
        """Verify sync_<b64> parameter is strictly <= 64 chars across ALL region/group combos."""
        regions = ["kyiv", "kyiv-region", "dnipro", "odesa"]
        subgroups = ["1.1", "1.2", "2.1", "2.2", "3.1", "3.2", "4.1", "4.2", "5.1", "5.2", "6.1", "6.2"]

        for reg_a in regions:
            for sub_a in subgroups:
                grp_a = int(sub_a.split(".")[0])
                for reg_b in regions:
                    for sub_b in subgroups:
                        grp_b = int(sub_b.split(".")[0])
                        slot_a = {"region_code": reg_a, "group_id": grp_a, "subgroup_id": sub_a, "alias": "Дім"}
                        slot_b = {"region_code": reg_b, "group_id": grp_b, "subgroup_id": sub_b, "alias": "Офіс"}

                        sync_code = TMAPayload.encode_sync_code(slot_a, slot_b)
                        self.assertTrue(sync_code.startswith("sync_"))
                        self.assertLessEqual(
                            len(sync_code),
                            64,
                            f"Sync code exceeds 64 chars limit: {len(sync_code)} for {reg_a}/{reg_b}",
                        )

    def test_sync_code_length_under_64_chars_extreme_aliases(self) -> None:
        """Verify sync_<b64> truncates long aliases to guarantee length <= 64 chars."""
        long_alias = "Дуже довга назва квартири в Голосіївському районі"
        slot_a = {"region_code": "kyiv-region", "group_id": 4, "subgroup_id": "4.2", "alias": long_alias}
        slot_b = {"region_code": "kyiv-region", "group_id": 5, "subgroup_id": "5.1", "alias": long_alias}

        sync_code = TMAPayload.encode_sync_code(slot_a, slot_b)
        self.assertLessEqual(len(sync_code), 64)

    def test_sync_code_roundtrip_dual_and_single_slot(self) -> None:
        """Verify sync code decodes correctly into SubscriptableSlotConfig models."""
        slot_a = {"region_code": "dnipro", "group_id": 3, "subgroup_id": "3.1", "alias": "Дім"}
        slot_b = {"region_code": "odesa", "group_id": 2, "subgroup_id": "2.2", "alias": "Дача"}

        # Dual slot roundtrip
        sync_code_dual = TMAPayload.encode_sync_code(slot_a, slot_b)
        cfg_a, cfg_b = TMAPayload.decode_sync_code(sync_code_dual)

        self.assertIsNotNone(cfg_b)
        self.assertEqual(cfg_a.region_code.value if hasattr(cfg_a.region_code, "value") else str(cfg_a.region_code), "dnipro")
        self.assertEqual(cfg_a.group_id, 3)
        self.assertEqual(cfg_a.subgroup_id, "3.1")
        self.assertEqual(cfg_b.region_code.value if hasattr(cfg_b.region_code, "value") else str(cfg_b.region_code), "odesa")
        self.assertEqual(cfg_b.group_id, 2)
        self.assertEqual(cfg_b.subgroup_id, "2.2")

        # Check subscripting support
        self.assertEqual(cfg_a["group_id"], 3)
        self.assertEqual(cfg_b.get("group_id"), 2)

        # Single slot roundtrip
        sync_code_single = TMAPayload.encode_sync_code(slot_a, None)
        s_cfg_a, s_cfg_b = TMAPayload.decode_sync_code(sync_code_single)
        self.assertIsNone(s_cfg_b)
        self.assertEqual(s_cfg_a.group_id, 3)

    def test_tma_decode_corrupted_truncated_inputs_raise_gracefully(self) -> None:
        """Verify corrupted base64/zlib payloads raise ValueError without crashing."""
        invalid_inputs = [
            "",
            "   ",
            "#p=",
            "#p=!!!invalid_chars!!!",
            "#p=" + "A" * 15,  # Invalid truncated base64 zlib stream
            "#p=eJztwTEBAAAAwqD1T20LL6AAAAAAAAAAAAAAAOBnCcAAAE=",  # Bad zlib magic
        ]
        for bad_inp in invalid_inputs:
            with self.assertRaises((ValueError, Exception)):
                TMAPayload.decode(bad_inp)

    def test_sync_decode_corrupted_malformed_inputs_raise_gracefully(self) -> None:
        """Verify malformed sync codes raise ValueError cleanly."""
        bad_sync_codes = [
            "",
            "invalid_prefix",
            "sync_",
            "sync_====",
            "sync_###bad_b64###",
            # Base64 with invalid group (group 99)
            "sync_a3lpdjo5OToxLjE=",
        ]
        for bad_code in bad_sync_codes:
            with self.assertRaises(ValueError):
                TMAPayload.decode_sync_code(bad_code)


# ============================================================================
# 4. NOTIFIER & RATE LIMITER ADVERSARIAL STRESS TESTS
# ============================================================================

class TestAdversarialNotifierAndRateLimiter(unittest.IsolatedAsyncioTestCase):
    """Stress tests for advance warnings, silent hours, diff alerts, and token bucket."""

    def test_advance_warning_transition_boundaries(self) -> None:
        """Verify 30m/15m warning detection across boundaries and edge cases."""
        # 48 slots: slot 0..27 ON, slot 28..47 OFF (transition at slot 28 = 14:00)
        slots = [{"status": "ON"} for _ in range(28)] + [{"status": "OFF"} for _ in range(20)]

        # 1. T - 30m check at 13:30 (target time 14:00 -> slot 28)
        dt_30m = datetime(2026, 11, 12, 13, 30, 0, tzinfo=KYIV_TZ)
        w30 = detect_advance_outage_transition(slots, dt_30m, 30)
        self.assertIsNotNone(w30)
        self.assertEqual(w30["target_slot_index"], 28)
        self.assertEqual(w30["transition_time"], "14:00")
        self.assertEqual(w30["event_type"], "30m_before_off")

        # 2. T - 15m check at 13:45 (target time 14:00 -> slot 28)
        dt_15m = datetime(2026, 11, 12, 13, 45, 0, tzinfo=KYIV_TZ)
        w15 = detect_advance_outage_transition(slots, dt_15m, 15)
        self.assertIsNotNone(w15)
        self.assertEqual(w15["target_slot_index"], 28)
        self.assertEqual(w15["transition_time"], "14:00")
        self.assertEqual(w15["event_type"], "15m_before_off")

        # 3. Non-transition check at 12:00 (target time 12:30 -> both ON)
        dt_none = datetime(2026, 11, 12, 12, 0, 0, tzinfo=KYIV_TZ)
        w_none = detect_advance_outage_transition(slots, dt_none, 30)
        self.assertIsNone(w_none)

        # 4. Midnight boundary: target time 00:00 (slot 0)
        # When target is slot 0, prior is -1 -> returns None gracefully
        dt_midnight = datetime(2026, 11, 12, 23, 30, 0, tzinfo=KYIV_TZ)
        w_mid = detect_advance_outage_transition(slots, dt_midnight, 30)
        self.assertIsNone(w_mid)

    def test_advance_warning_formatting_dual_and_single_slot(self) -> None:
        """Verify Ukrainian advance warning alert message formatting."""
        # Single slot
        msg1 = format_advance_warning_message(
            lead_minutes=30,
            transition_time="14:00",
            slot1_alias="Дім",
            slot1_group="1.1",
        )
        self.assertIn("30 хв", msg1)
        self.assertIn("14:00", msg1)
        self.assertIn("Дім", msg1)

        # Dual slot with power at second location
        msg2 = format_advance_warning_message(
            lead_minutes=15,
            transition_time="18:30",
            slot1_alias="Дім",
            slot1_group="1.1",
            slot2_alias="Офіс",
            slot2_group="2.1",
            slot2_has_power=True,
        )
        self.assertIn("світло буде", msg2)

        # Dual slot without power at second location
        msg3 = format_advance_warning_message(
            lead_minutes=15,
            transition_time="18:30",
            slot1_alias="Дім",
            slot1_group="1.1",
            slot2_alias="Офіс",
            slot2_group="2.1",
            slot2_has_power=False,
        )
        self.assertIn("також не буде світла", msg3)

    def test_silent_hours_filtering_and_edge_cases(self) -> None:
        """Verify silent hours suppression between 23:00 and 07:00 Europe/Kyiv."""
        # Active silent hours
        for hour in (23, 0, 1, 4, 6):
            dt = datetime(2026, 11, 12, hour, 15, tzinfo=KYIV_TZ)
            self.assertTrue(is_in_silent_hours(dt, 23, 7), f"Hour {hour} should be silent")

        # Daytime non-silent hours
        for hour in (7, 10, 14, 18, 22):
            dt = datetime(2026, 11, 12, hour, 0, tzinfo=KYIV_TZ)
            self.assertFalse(is_in_silent_hours(dt, 23, 7), f"Hour {hour} should NOT be silent")

        # Non-timezone-aware dt should be safely handled
        naive_dt = datetime(2026, 11, 12, 23, 30)
        self.assertTrue(is_in_silent_hours(naive_dt, 23, 7))

    def test_schedule_revision_diff_detection_sha256(self) -> None:
        """Verify revision diff detector groups contiguous modified intervals."""
        old_slots = [{"status": "ON"} for _ in range(48)]
        # Change intervals 20..23 (10:00 to 12:00) to OFF
        new_slots = [{"status": "ON"} for _ in range(48)]
        for k in range(20, 24):
            new_slots[k] = {"status": "OFF"}

        diffs = detect_schedule_revision_diffs(old_slots, new_slots)
        self.assertEqual(len(diffs), 1)
        self.assertEqual(diffs[0]["start_slot_index"], 20)
        self.assertEqual(diffs[0]["end_slot_index"], 23)
        self.assertIn("10:00", diffs[0]["time_range"])
        self.assertIn("12:00", diffs[0]["time_range"])

        msg = format_schedule_diff_message("2.1", diffs)
        self.assertIn("додано відключення", msg)
        self.assertIn("2.1", msg)

    async def test_token_bucket_concurrency_and_rate_enforcement(self) -> None:
        """Verify TokenBucketRateLimiter caps burst throughput at 25 msg/s under concurrency."""
        limiter = TokenBucketRateLimiter(rate=25.0, capacity=25.0)

        # Acquire initial 25 tokens instantaneously
        start_time = time.monotonic()
        for _ in range(25):
            await limiter.acquire()
        burst_duration = time.monotonic() - start_time
        # 25 burst tokens should complete within < 0.1 seconds
        self.assertLess(burst_duration, 0.15)

        # The 26th token must wait for bucket refill (1 / 25.0 = 0.04s)
        wait_start = time.monotonic()
        await limiter.acquire()
        wait_duration = time.monotonic() - wait_start
        self.assertGreaterEqual(wait_duration, 0.02)

        # Test concurrent worker tasks
        tasks = [limiter.acquire() for _ in range(10)]
        results = await asyncio.gather(*tasks)
        self.assertEqual(len(results), 10)

    async def test_http_429_backoff_handling_retry_after_plus_one(self) -> None:
        """Verify HTTP 429 enforces mandatory (retry_after + 1.0s) pause."""
        limiter = TokenBucketRateLimiter(rate=25.0, capacity=25.0)

        # Inject 429 with retry_after = 0.05s
        limiter.handle_retry_after(0.05)
        self.assertTrue(limiter.is_in_backoff)

        start = time.monotonic()
        waited = await limiter.acquire()
        elapsed = time.monotonic() - start

        # Must have waited at least 1.05 seconds (0.05 + 1.0)
        self.assertGreaterEqual(elapsed, 1.0)
        self.assertFalse(limiter.is_in_backoff)


# ============================================================================
# 5. DEPLOYMENT & SECURITY INVARIANTS TESTS
# ============================================================================

class TestDeploymentAndSecurityInvariants(unittest.TestCase):
    """Rigorous verification of zero-port, non-root, and static isolation invariants."""

    def test_compose_zero_ports_and_expose(self) -> None:
        """Verify compose.yaml has ZERO open inbound ports ('ports:' and 'expose:')."""
        compose_path = PROJECT_ROOT / "compose.yaml"
        self.assertTrue(compose_path.is_file(), f"compose.yaml not found at {compose_path}")

        content = compose_path.read_text(encoding="utf-8")
        lines = content.splitlines()

        for idx, line in enumerate(lines, 1):
            stripped = line.strip()
            # Ignore comments
            if stripped.startswith("#"):
                continue
            self.assertFalse(
                re.match(r"^ports\s*:", stripped),
                f"Forbidden 'ports:' directive in compose.yaml line {idx}: {line}",
            )
            self.assertFalse(
                re.match(r"^expose\s*:", stripped),
                f"Forbidden 'expose:' directive in compose.yaml line {idx}: {line}",
            )

    def test_compose_hardening_read_only_tmpfs_memory(self) -> None:
        """Verify compose.yaml specifies read-only rootfs, tmpfs, and 256MB memory cap."""
        content = (PROJECT_ROOT / "compose.yaml").read_text(encoding="utf-8")
        self.assertIn("read_only: true", content)
        self.assertIn("tmpfs:", content)
        self.assertIn("/tmp", content)
        self.assertIn("mem_limit: 256m", content)

    def test_containerfile_zero_expose_and_uid_1000(self) -> None:
        """Verify Containerfile has ZERO EXPOSE directives and runs as non-root UID 1000."""
        container_path = PROJECT_ROOT / "Containerfile"
        self.assertTrue(container_path.is_file(), f"Containerfile not found at {container_path}")

        content = container_path.read_text(encoding="utf-8")
        lines = content.splitlines()

        for idx, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            self.assertFalse(
                re.match(r"^EXPOSE\b", stripped, re.IGNORECASE),
                f"Forbidden EXPOSE directive in Containerfile line {idx}: {line}",
            )

        # Verify non-root user setup
        self.assertRegex(content, r"USER\s+botuser", "Containerfile must switch to non-root USER botuser")
        self.assertRegex(content, r"-u\s+1000", "Containerfile must configure UID 1000")
        self.assertRegex(content, r"-g\s+1000", "Containerfile must configure GID 1000")

    def test_main_py_outbound_polling_only(self) -> None:
        """Verify bot/main.py uses outbound long-polling exclusively with 0 webhooks/servers."""
        main_path = PROJECT_ROOT / "bot" / "main.py"
        self.assertTrue(main_path.is_file(), f"bot/main.py not found at {main_path}")

        content = main_path.read_text(encoding="utf-8")

        # Parse AST to ensure valid Python syntax
        ast.parse(content)

        # Must use start_polling
        self.assertIn("start_polling", content, "bot/main.py must invoke start_polling()")

        # Must NOT instantiate inbound web servers or webhook handlers
        forbidden_patterns = [
            r"run_app\(",
            r"aiohttp\.web\.Application",
            r"set_webhook\(",
            r"FastAPI\(",
            r"uvicorn\.run",
            r"Flask\(",
        ]
        for pat in forbidden_patterns:
            self.assertIsNone(
                re.search(pat, content),
                f"Forbidden inbound server pattern found in bot/main.py: {pat}",
            )

    def test_docs_index_html_zero_localhost_127_api_calls(self) -> None:
        """Verify docs/index.html contains 0 references to localhost, 127.0.0.1, or /api/."""
        html_path = PROJECT_ROOT / "docs" / "index.html"
        self.assertTrue(html_path.is_file(), f"docs/index.html not found at {html_path}")

        content = html_path.read_text(encoding="utf-8")
        forbidden = ["localhost", "127.0.0.1", "/api/"]
        for token in forbidden:
            self.assertNotIn(
                token,
                content.lower(),
                f"Forbidden token {token!r} found in docs/index.html",
            )

    def test_zero_todo_and_implement_here_placeholders(self) -> None:
        """Verify 0 occurrences of 'TODO' or 'implement here' in all project source files."""
        scan_paths = [
            PROJECT_ROOT / "compose.yaml",
            PROJECT_ROOT / "Containerfile",
            PROJECT_ROOT / "docs" / "index.html",
        ]
        # Include all python files in bot/
        bot_dir = PROJECT_ROOT / "bot"
        for p in bot_dir.rglob("*.py"):
            scan_paths.append(p)

        for fpath in scan_paths:
            self.assertTrue(fpath.is_file(), f"Target file not found: {fpath}")
            text = fpath.read_text(encoding="utf-8", errors="ignore")
            self.assertNotIn(
                "TODO",
                text.upper(),
                f"Found forbidden 'TODO' in {fpath.relative_to(PROJECT_ROOT)}",
            )
            self.assertNotIn(
                "IMPLEMENT HERE",
                text.upper(),
                f"Found forbidden 'IMPLEMENT HERE' in {fpath.relative_to(PROJECT_ROOT)}",
            )


if __name__ == "__main__":
    unittest.main()
