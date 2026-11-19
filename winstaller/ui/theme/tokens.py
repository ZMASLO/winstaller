"""Colour tokens for the dark (default) and light themes.

Contrast (WCAG 2.x) of text tokens against ``bg`` was checked when choosing them:
dark ``text`` 17.0, ``muted`` 5.6, ``accent_fg`` 11.9, ``error_fg`` 5.3; light ``text`` 16.7,
``muted`` 5.9, ``accent_fg`` 5.5, ``error_fg`` 5.5; ``on_accent`` on ``accent`` 10.3.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Palette:
    name: str
    is_dark: bool
    bg: str  # window background
    surface: str  # navigation, bars, cards
    surface2: str  # hover / raised
    border: str  # 1 px separators
    border_strong: str  # outlines of secondary controls
    text: str
    muted: str  # secondary text (descriptions, metadata)
    faint: str  # disabled text
    accent: str  # fills: primary button, checkbox, progress chunk
    accent_hover: str
    accent_fg: str  # accent used as text or thin lines on ``bg``
    on_accent: str  # text on ``accent``
    error: str  # error lines / borders
    error_fg: str  # error text on ``bg``
    selection: str  # text selection background


DARK = Palette(
    name="dark",
    is_dark=True,
    bg="#0D0D0D",
    surface="#141414",
    surface2="#1C1C1C",
    border="#262626",
    border_strong="#3A3A38",
    text="#F0F0EE",
    muted="#8A8A85",
    faint="#5C5C58",
    accent="#E9C84A",
    accent_hover="#F3D86E",
    accent_fg="#E9C84A",
    on_accent="#231D02",
    error="#C4453A",
    error_fg="#E05A4E",
    selection="#4A3F12",
)

LIGHT = Palette(
    name="light",
    is_dark=False,
    bg="#F4F4F1",
    surface="#FBFBF9",
    surface2="#E9E9E4",
    border="#D8D8D3",
    border_strong="#B9B9B3",
    text="#141414",
    muted="#5E5E59",
    faint="#9A9A94",
    accent="#E9C84A",
    accent_hover="#DDB935",
    accent_fg="#7A5F00",
    on_accent="#231D02",
    error="#B3362C",
    error_fg="#B3362C",
    selection="#F1DE95",
)


def palette_for(dark: bool) -> Palette:
    return DARK if dark else LIGHT
