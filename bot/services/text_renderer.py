"""Text Renderer Re-export Proxy.

Re-exports TextRenderer and render_timeline_text from bot.rendering.text.
"""

from __future__ import annotations

from bot.rendering.text import TextRenderer, render_timeline_text

__all__ = ["TextRenderer", "render_timeline_text"]
