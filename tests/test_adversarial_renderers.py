"""
tests/test_adversarial_renderers.py - Adversarial Stress & Fuzzing Harness for Milestone 3 Renderers.

Empirically challenges bot/rendering/text.py and bot/rendering/image.py:
1. Strict Mobile Width Invariant (len(line) <= 28) under adversarial alias lengths (100+ chars),
   Unicode zero-width spaces, complex emoji sequences, combining marks, BiDi overrides, and extreme schedules.
2. PNG Card Generator specifications (1200x640 px, RGBA mode, 144 DPI pHYs chunk) under extreme schedules
   (100% ON, 100% OFF, 100% EMERGENCY, 100% MAYBE, alternating patterns, single-slot fallback).
3. Fallback pure-Python PNG generator validation.
"""

from __future__ import annotations

import io
import struct
import unittest
from typing import Any, Callable, Dict, List, Optional, Tuple

from bot.rendering.text import TextRenderer, render_timeline_text
from bot.rendering.image import ImageRenderer, render_timeline_png, _render_fallback_png


def _parse_png_metadata(data: bytes) -> Tuple[int, int, int, int, Optional[float]]:
    """
    Parse PNG header and DPI.
    Returns: (width, height, bit_depth, color_type, dpi)
    """
    if len(data) < 33:
        raise ValueError(f"PNG buffer too short ({len(data)} bytes)")
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("Invalid PNG magic bytes")

    # IHDR
    w, h, bit_d, col_t = struct.unpack(">IIBB", data[16:26])

    # Search for pHYs chunk
    dpi: Optional[float] = None
    offset = 8
    while offset + 8 <= len(data):
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        ctype = data[offset + 4:offset + 8]
        if ctype == b"pHYs" and length >= 9:
            ppu_x, ppu_y, unit = struct.unpack(">IIB", data[offset + 8:offset + 17])
            if unit == 1:  # meters
                dpi = ppu_x * 0.0254
            break
        offset += 8 + length + 4

    return w, h, bit_d, col_t, dpi


def _make_timeline(status_a: Any, status_b: Any, joint: Any, count: int = 48) -> List[Dict[str, Any]]:
    """Helper to generate timeline intervals."""
    intervals = []
    for i in range(count):
        sa = status_a(i) if callable(status_a) else status_a
        sb = status_b(i) if callable(status_b) else status_b
        js = joint(i) if callable(joint) else joint
        intervals.append({
            "interval_index": i,
            "start_time": f"{i // 2:02d}:{(i % 2) * 30:02d}",
            "end_time": f"{(i + 1) // 2:02d}:{((i + 1) % 2) * 30:02d}" if i < count - 1 else "24:00",
            "status_a": sa,
            "status_b": sb,
            "joint_status": js,
        })
    return intervals


class TestTextRendererAdversarial(unittest.TestCase):
    """Adversarial stress-testing suite for bot/rendering/text.py."""

    def assert_all_lines_le_28(self, text: str, scenario_desc: str = "") -> None:
        """Rigorously assert that EVERY line in text table is <= 28 characters."""
        lines = text.splitlines()
        self.assertGreater(len(lines), 0, f"Rendered text is empty: {scenario_desc}")
        violations = []
        for idx, line in enumerate(lines, 1):
            clean = line.rstrip("\r\n")
            if len(clean) > 28:
                violations.append((idx, len(clean), clean))

        if violations:
            err_msg = (
                f"WIDTH INVARIANT VIOLATION in {scenario_desc} ({len(violations)} lines > 28 chars):\n" +
                "\n".join(f"  Line {i} ({length} chars): {repr(content)}" for i, length, content in violations)
            )
            self.fail(err_msg)

    def test_extreme_alias_lengths_never_exceed_28(self) -> None:
        """Test aliases from 50 to 1000 characters in ASCII and Cyrillic."""
        extreme_aliases = [
            "A" * 50,
            "B" * 100,
            "C" * 250,
            "D" * 1000,
            "Дім" * 35,
            "Офіс" * 35,
            "МояУлюбленаКвартираУЦентріКиєваЗГенераторомТаСтарлінком" * 5,
        ]
        timeline = _make_timeline("ON", "OFF", "GROUP1_ONLY")
        for alias in extreme_aliases:
            payload = {
                "comparison_date": "11.11.2026",
                "slot_a": {"alias": alias},
                "slot_b": {"alias": alias},
                "timeline": timeline,
                "summary": {"both_on_hours": 0.0, "group1_only_hours": 24.0, "both_off_hours": 0.0},
            }
            txt = render_timeline_text(payload)
            self.assert_all_lines_le_28(txt, f"Alias length {len(alias)}")

    def test_unicode_emojis_and_zero_width_never_exceed_28(self) -> None:
        """Test emojis, zero-width chars, combining marks, and BiDi overrides."""
        adversarial_strings = [
            "🔥" * 40,
            "👨‍👩‍👧‍👦" * 15,
            "🇺🇦" * 30,
            "\u200b\u200c\u200d\ufeff" * 20,
            "Z̶a̸l̶g̴o̸" * 15,
            "\u202e\u202d\u202c\u202b" * 15,
            "🏠" + "\u200b" * 50 + "🏢",
            "Дім 🇺🇦 123 !@#$%^&*()",
            "\t\t   spaces   \t\t",
            "Multi\nLine\r\nAlias",
        ]
        timeline = _make_timeline("ON", "ON", "BOTH_ON")
        for adv in adversarial_strings:
            payload = {
                "comparison_date": "11.11.2026",
                "slot_a": {"alias": adv},
                "slot_b": {"alias": adv},
                "timeline": timeline,
                "summary": {"both_on_hours": 24.0, "both_off_hours": 0.0},
            }
            txt = render_timeline_text(payload)
            self.assert_all_lines_le_28(txt, f"Adversarial string {repr(adv[:15])}")

    def test_extreme_schedules_never_exceed_28(self) -> None:
        """Test all-ON, all-OFF, all-EMERGENCY, all-MAYBE, and 48-transition alternating schedule."""
        patterns = {
            "all_on": _make_timeline("ON", "ON", "BOTH_ON"),
            "all_off": _make_timeline("OFF", "OFF", "BOTH_OFF"),
            "all_emergency": _make_timeline("EMERGENCY", "EMERGENCY", "EMERGENCY"),
            "all_maybe": _make_timeline("MAYBE", "MAYBE", "BOTH_MAYBE"),
            "alternating_48": _make_timeline(
                lambda i: "ON" if i % 2 == 0 else "OFF",
                lambda i: "OFF" if i % 2 == 0 else "ON",
                lambda i: "GROUP1_ONLY" if i % 2 == 0 else "GROUP2_ONLY",
            ),
            "combinatorial_9": _make_timeline(
                lambda i: ["ON", "OFF", "MAYBE"][i % 3],
                lambda i: ["ON", "OFF", "MAYBE"][(i // 3) % 3],
                lambda i: [
                    "BOTH_ON", "GROUP1_ON_GROUP2_MAYBE", "GROUP1_ONLY",
                    "GROUP1_MAYBE_GROUP2_ON", "BOTH_MAYBE", "GROUP1_MAYBE_GROUP2_OFF",
                    "GROUP2_ONLY", "GROUP1_OFF_GROUP2_MAYBE", "BOTH_OFF",
                ][i % 9],
            ),
        }
        for name, tl in patterns.items():
            payload = {
                "comparison_date": "11.11.2026",
                "slot_a": {"alias": "Дім"},
                "slot_b": {"alias": "Офіс"},
                "timeline": tl,
                "summary": {
                    "both_on_hours": 6.0,
                    "group1_only_hours": 6.0,
                    "group2_only_hours": 6.0,
                    "both_off_hours": 6.0,
                    "uncertain_hours": 0.0,
                    "current_joint_status": "BOTH_ON",
                    "minutes_to_next_transition": 30,
                    "next_transition_time": "14:00",
                },
            }
            txt = render_timeline_text(payload)
            self.assert_all_lines_le_28(txt, f"Schedule pattern {name}")

    def test_single_slot_mode_never_exceed_28(self) -> None:
        """Test single-slot mode (slot_b is None) under extreme inputs."""
        timeline = _make_timeline("ON", "ON", "ON")
        for alias in ["ДужеДовгаНазваСлоту" * 5, "🔥" * 20, "Single"]:
            payload = {
                "comparison_date": "11.11.2026",
                "slot_a": {"alias": alias},
                "slot_b": None,
                "timeline": timeline,
                "summary": {"group1_only_hours": 24.0, "both_off_hours": 0.0, "uncertain_hours": 0.0},
            }
            txt = render_timeline_text(payload)
            self.assert_all_lines_le_28(txt, f"Single-slot alias {repr(alias[:10])}")

    def test_summary_and_countdown_overflow_never_exceed_28(self) -> None:
        """Test floating-point hour overflows, large minutes, and long date strings."""
        payload = {
            "comparison_date": "2026-11-11T23:59:59.999999+02:00",
            "is_emergency": True,
            "stale": True,
            "slot_a": {"alias": "A" * 50},
            "slot_b": {"alias": "B" * 50},
            "timeline": _make_timeline("EMERGENCY", "EMERGENCY", "EMERGENCY"),
            "summary": {
                "both_on_hours": 12.345678901234,
                "group1_only_hours": 9999999.99,
                "group2_only_hours": 0.0000000001,
                "both_off_hours": -12.5,
                "uncertain_hours": 3.1415926535,
                "current_joint_status": "EMERGENCY",
                "minutes_to_next_transition": 999999,
                "next_transition_time": "23:59:59.999",
            },
        }
        txt = render_timeline_text(payload)
        self.assert_all_lines_le_28(txt, "Summary float overflow")

    def test_empty_timeline_and_empty_payload(self) -> None:
        """Test empty timeline and empty dictionary payload without crashing."""
        for payload in [{}, {"timeline": []}, {"slot_a": {}, "slot_b": None, "timeline": []}]:
            txt = render_timeline_text(payload)
            self.assert_all_lines_le_28(txt, f"Empty payload {payload}")


class TestImageRendererAdversarial(unittest.TestCase):
    """Adversarial stress-testing suite for bot/rendering/image.py."""

    def assert_valid_png(self, data: bytes, desc: str = "") -> None:
        """Verify PNG buffer validity, exact 1200x640 dimensions, RGBA mode, and 144 DPI."""
        self.assertIsInstance(data, bytes, f"{desc}: Output must be bytes")
        self.assertGreater(len(data), 1000, f"{desc}: PNG too small ({len(data)} bytes)")
        w, h, bit_d, col_t, dpi = _parse_png_metadata(data)
        self.assertEqual(w, 1200, f"{desc}: Width {w} != 1200")
        self.assertEqual(h, 640, f"{desc}: Height {h} != 640")
        self.assertEqual(bit_d, 8, f"{desc}: Bit depth {bit_d} != 8")
        self.assertEqual(col_t, 6, f"{desc}: Color type {col_t} != 6 (RGBA)")
        self.assertIsNotNone(dpi, f"{desc}: pHYs chunk missing (144 DPI)")
        self.assertAlmostEqual(dpi, 144.0, delta=1.5, msg=f"{desc}: DPI {dpi} != 144")

    def test_extreme_schedules_pillow(self) -> None:
        """Verify Pillow PNG generation across extreme schedules."""
        schedules = [
            ("all_on", _make_timeline("ON", "ON", "BOTH_ON")),
            ("all_off", _make_timeline("OFF", "OFF", "BOTH_OFF")),
            ("all_emergency", _make_timeline("EMERGENCY", "EMERGENCY", "EMERGENCY")),
            ("all_maybe", _make_timeline("MAYBE", "MAYBE", "BOTH_MAYBE")),
            ("alternating", _make_timeline(
                lambda i: "ON" if i % 2 == 0 else "OFF",
                lambda i: "OFF" if i % 2 == 0 else "ON",
                lambda i: "GROUP1_ONLY" if i % 2 == 0 else "GROUP2_ONLY",
            )),
        ]
        for name, tl in schedules:
            payload = {
                "comparison_date": "2026-11-11",
                "slot_a": {"alias": "Дім"},
                "slot_b": {"alias": "Офіс"},
                "timeline": tl,
                "summary": {"current_slot_index": 20, "both_on_hours": 10.0, "both_off_hours": 14.0},
            }
            png = render_timeline_png(payload)
            self.assert_valid_png(png, f"Pillow extreme schedule {name}")

    def test_extreme_schedules_fallback_png(self) -> None:
        """Verify pure-Python fallback PNG generator across extreme schedules."""
        schedules = [
            ("all_on", _make_timeline("ON", "ON", "BOTH_ON")),
            ("all_off", _make_timeline("OFF", "OFF", "BOTH_OFF")),
            ("all_emergency", _make_timeline("EMERGENCY", "EMERGENCY", "EMERGENCY")),
        ]
        for name, tl in schedules:
            payload = {
                "comparison_date": "2026-11-11",
                "slot_a": {"alias": "Дім"},
                "slot_b": {"alias": "Офіс"},
                "timeline": tl,
                "summary": {"current_slot_index": 20, "both_on_hours": 10.0, "both_off_hours": 14.0},
            }
            png = _render_fallback_png(payload)
            self.assert_valid_png(png, f"Fallback PNG extreme schedule {name}")

    def test_png_extreme_alias_lengths(self) -> None:
        """Verify PNG rendering survives 100+ char aliases and complex Unicode without crashing."""
        adversarial_aliases = [
            "A" * 150,
            "Б" * 150,
            "🔥" * 50,
            "\u200b\u200c\u200d\ufeff" * 20,
            "Z̶a̸l̶g̴o̸" * 15,
            "SELECT * FROM users; --",
        ]
        for alias in adversarial_aliases:
            payload = {
                "comparison_date": "2026-11-11",
                "slot_a": {"alias": alias},
                "slot_b": {"alias": alias},
                "timeline": [],
                "summary": {"current_slot_index": 12},
            }
            png_pil = render_timeline_png(payload)
            self.assert_valid_png(png_pil, f"Pillow alias {repr(alias[:15])}")
            png_fb = _render_fallback_png(payload)
            self.assert_valid_png(png_fb, f"Fallback alias {repr(alias[:15])}")

    def test_png_single_slot_mode(self) -> None:
        """Verify PNG rendering with single slot mode (slot_b is None)."""
        payload = {
            "comparison_date": "2026-11-11",
            "slot_a": {"alias": "ЄдинийСлот"},
            "slot_b": None,
            "timeline": _make_timeline("ON", "ON", "ON"),
            "summary": {"current_slot_index": 10, "group1_only_hours": 24.0, "both_off_hours": 0.0},
        }
        png_pil = render_timeline_png(payload)
        self.assert_valid_png(png_pil, "Pillow single slot mode")
        png_fb = _render_fallback_png(payload)
        self.assert_valid_png(png_fb, "Fallback single slot mode")

    def test_current_slot_index_boundaries(self) -> None:
        """Verify Now needle handles slot indices at boundaries (-10, 0, 47, 100)."""
        for idx in [-10, 0, 23, 47, 48, 100]:
            payload = {
                "timeline": [],
                "summary": {"current_slot_index": idx},
            }
            png = render_timeline_png(payload)
            self.assert_valid_png(png, f"Slot index {idx}")


if __name__ == "__main__":
    unittest.main()
