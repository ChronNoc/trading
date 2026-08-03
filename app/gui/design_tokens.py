"""Theme-neutral layout tokens and legacy palette constants for the GUI.

The active dark and light reusable-widget palettes live in :mod:`app.gui.theme`.
This module retains shared spacing, typography, elevation, and compatibility
constants used by the dashboard component system.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

# =============================================================================
# Color Palette
# =============================================================================

# Base layers (navy to near-black)
COLOR_BASE_DARKEST: Final[str] = "#0a0e1a"  # Deepest background
COLOR_BASE_DARKER: Final[str] = "#0f1419"  # Secondary background
COLOR_BASE_DARK: Final[str] = "#151922"  # Card/panel background
COLOR_BASE_MID: Final[str] = "#1e2633"  # Elevated cards
COLOR_BASE_LIGHT: Final[str] = "#2a3544"  # Hover/active states

# Glass effect overlays
COLOR_GLASS_OVERLAY: Final[str] = "rgba(255, 255, 255, 0.03)"
COLOR_GLASS_BORDER: Final[str] = "rgba(255, 255, 255, 0.08)"
COLOR_GLASS_HOVER: Final[str] = "rgba(255, 255, 255, 0.06)"

# Primary (electric blue)
COLOR_PRIMARY: Final[str] = "#0ea5e9"
COLOR_PRIMARY_LIGHT: Final[str] = "#38bdf8"
COLOR_PRIMARY_DARK: Final[str] = "#0284c7"
COLOR_PRIMARY_GLOW: Final[str] = "rgba(14, 165, 233, 0.25)"

# Accent (cyan)
COLOR_ACCENT: Final[str] = "#22d3ee"
COLOR_ACCENT_LIGHT: Final[str] = "#67e8f9"
COLOR_ACCENT_DARK: Final[str] = "#06b6d4"
COLOR_ACCENT_GLOW: Final[str] = "rgba(34, 211, 238, 0.25)"

# Highlight (violet)
COLOR_HIGHLIGHT: Final[str] = "#8b5cf6"
COLOR_HIGHLIGHT_LIGHT: Final[str] = "#a78bfa"
COLOR_HIGHLIGHT_DARK: Final[str] = "#7c3aed"
COLOR_HIGHLIGHT_GLOW: Final[str] = "rgba(139, 92, 246, 0.25)"

# Success (emerald)
COLOR_SUCCESS: Final[str] = "#10b981"
COLOR_SUCCESS_LIGHT: Final[str] = "#34d399"
COLOR_SUCCESS_DARK: Final[str] = "#059669"
COLOR_SUCCESS_GLOW: Final[str] = "rgba(16, 185, 129, 0.25)"

# Warning (amber)
COLOR_WARNING: Final[str] = "#f59e0b"
COLOR_WARNING_LIGHT: Final[str] = "#fbbf24"
COLOR_WARNING_DARK: Final[str] = "#d97706"

# Locked/safety gate (amber family, semantically distinct from warning)
COLOR_LOCKED_DARK: Final[str] = "#49391b"  # background for locked state
COLOR_LOCKED_LIGHT: Final[str] = "#ffd98c"  # text for locked state
COLOR_LOCKED_BORDER: Final[str] = "#a57b25"  # border for locked state
COLOR_LOCKED_GLOW: Final[str] = "rgba(255, 217, 140, 0.25)"

# Error (red)
COLOR_ERROR: Final[str] = "#ef4444"
COLOR_ERROR_LIGHT: Final[str] = "#f87171"
COLOR_ERROR_DARK: Final[str] = "#dc2626"
COLOR_ERROR_GLOW: Final[str] = "rgba(239, 68, 68, 0.25)"

# Text
COLOR_TEXT_PRIMARY: Final[str] = "#f8fafc"  # Near white
COLOR_TEXT_SECONDARY: Final[str] = "#cbd5e1"  # Muted
COLOR_TEXT_TERTIARY: Final[str] = "#94a3b8"  # Subtle
COLOR_TEXT_DISABLED: Final[str] = "#64748b"  # Very subtle

# Chart/data colors (high contrast, accessible)
COLOR_CHART_BLUE: Final[str] = "#3b82f6"
COLOR_CHART_CYAN: Final[str] = "#06b6d4"
COLOR_CHART_VIOLET: Final[str] = "#8b5cf6"
COLOR_CHART_PINK: Final[str] = "#ec4899"
COLOR_CHART_EMERALD: Final[str] = "#10b981"
COLOR_CHART_AMBER: Final[str] = "#f59e0b"
COLOR_CHART_ORANGE: Final[str] = "#f97316"

# =============================================================================
# Spacing (8px grid system)
# =============================================================================

SPACE_XXS: Final[int] = 4
SPACE_XS: Final[int] = 8
SPACE_SM: Final[int] = 12
SPACE_MD: Final[int] = 16
SPACE_LG: Final[int] = 24
SPACE_XL: Final[int] = 32
SPACE_XXL: Final[int] = 48
SPACE_HUGE: Final[int] = 64

# =============================================================================
# Typography
# =============================================================================

# Font families (system fonts for performance)
FONT_FAMILY_SANS: Final[str] = (
    '-apple-system, BlinkMacSystemFont, "Segoe UI", '
    'Roboto, "Helvetica Neue", Arial, sans-serif'
)
FONT_FAMILY_MONO: Final[str] = (
    '"SF Mono", "Cascadia Code", "Fira Code", '
    'Consolas, "Liberation Mono", monospace'
)

# Font sizes
FONT_SIZE_XS: Final[int] = 11
FONT_SIZE_SM: Final[int] = 13
FONT_SIZE_MD: Final[int] = 15
FONT_SIZE_LG: Final[int] = 18
FONT_SIZE_XL: Final[int] = 24
FONT_SIZE_XXL: Final[int] = 32
FONT_SIZE_HUGE: Final[int] = 48

# Font weights
FONT_WEIGHT_NORMAL: Final[int] = 400
FONT_WEIGHT_MEDIUM: Final[int] = 500
FONT_WEIGHT_SEMIBOLD: Final[int] = 600
FONT_WEIGHT_BOLD: Final[int] = 700

# Line heights
LINE_HEIGHT_TIGHT: Final[float] = 1.2
LINE_HEIGHT_NORMAL: Final[float] = 1.5
LINE_HEIGHT_RELAXED: Final[float] = 1.75

# =============================================================================
# Border Radius
# =============================================================================

RADIUS_SM: Final[int] = 4  # Badges, small controls
RADIUS_MD: Final[int] = 6  # Buttons, inputs
RADIUS_LG: Final[int] = 8  # Cards, panels
RADIUS_XL: Final[int] = 12  # Large containers
RADIUS_FULL: Final[int] = 9999  # Pills, circular

# =============================================================================
# Shadows (elevation system)
# =============================================================================

SHADOW_SM: Final[str] = "0 1px 2px 0 rgba(0, 0, 0, 0.25)"
SHADOW_MD: Final[str] = "0 4px 6px -1px rgba(0, 0, 0, 0.3), 0 2px 4px -2px rgba(0, 0, 0, 0.25)"
SHADOW_LG: Final[str] = "0 10px 15px -3px rgba(0, 0, 0, 0.4), 0 4px 6px -4px rgba(0, 0, 0, 0.3)"
SHADOW_XL: Final[str] = "0 20px 25px -5px rgba(0, 0, 0, 0.5), 0 8px 10px -6px rgba(0, 0, 0, 0.4)"
SHADOW_GLOW_PRIMARY: Final[str] = f"0 0 20px {COLOR_PRIMARY_GLOW}"
SHADOW_GLOW_ACCENT: Final[str] = f"0 0 20px {COLOR_ACCENT_GLOW}"
SHADOW_GLOW_SUCCESS: Final[str] = f"0 0 20px {COLOR_SUCCESS_GLOW}"
SHADOW_GLOW_ERROR: Final[str] = f"0 0 20px {COLOR_ERROR_GLOW}"

# =============================================================================
# Borders
# =============================================================================

BORDER_WIDTH_THIN: Final[int] = 1
BORDER_WIDTH_MEDIUM: Final[int] = 2
BORDER_WIDTH_THICK: Final[int] = 3

# =============================================================================
# Transitions
# =============================================================================

TRANSITION_FAST: Final[str] = "150ms"
TRANSITION_NORMAL: Final[str] = "250ms"
TRANSITION_SLOW: Final[str] = "400ms"
TRANSITION_EASE: Final[str] = "cubic-bezier(0.4, 0, 0.2, 1)"

# =============================================================================
# Z-Index Layers
# =============================================================================

Z_INDEX_BASE: Final[int] = 0
Z_INDEX_DROPDOWN: Final[int] = 100
Z_INDEX_STICKY: Final[int] = 200
Z_INDEX_OVERLAY: Final[int] = 300
Z_INDEX_MODAL: Final[int] = 400
Z_INDEX_POPOVER: Final[int] = 500
Z_INDEX_TOOLTIP: Final[int] = 600

# =============================================================================
# Component-Specific Tokens
# =============================================================================


@dataclass(frozen=True, slots=True)
class CardStyle:
    """Theme-neutral layout and elevation tokens for dashboard cards."""

    padding: int = SPACE_MD
    shadow_blur_radius: int = 16
    shadow_offset_y: int = 4
    shadow_alpha: int = 80


@dataclass(frozen=True, slots=True)
class ButtonStyle:
    """Button variant styling."""

    background: str
    background_hover: str
    text: str
    border: str | None = None
    glow: str | None = None
    padding_x: int = SPACE_MD
    padding_y: int = SPACE_SM
    font_weight: int = FONT_WEIGHT_MEDIUM
    border_radius: int = RADIUS_MD


@dataclass(frozen=True, slots=True)
class BadgeStyle:
    """Status badge styling."""

    background: str
    text: str
    border: str | None = None
    glow: str | None = None
    padding_x: int = SPACE_XS
    padding_y: int = SPACE_XXS
    font_size: int = FONT_SIZE_XS
    font_weight: int = FONT_WEIGHT_SEMIBOLD
    border_radius: int = RADIUS_SM


# Button variants
BUTTON_PRIMARY = ButtonStyle(
    background=COLOR_PRIMARY,
    background_hover=COLOR_PRIMARY_LIGHT,
    text=COLOR_TEXT_PRIMARY,
    glow=SHADOW_GLOW_PRIMARY,
)

BUTTON_SECONDARY = ButtonStyle(
    background=COLOR_BASE_LIGHT,
    background_hover=COLOR_GLASS_HOVER,
    text=COLOR_TEXT_PRIMARY,
    border=COLOR_GLASS_BORDER,
)

BUTTON_DANGER = ButtonStyle(
    background=COLOR_ERROR,
    background_hover=COLOR_ERROR_LIGHT,
    text=COLOR_TEXT_PRIMARY,
    glow=SHADOW_GLOW_ERROR,
)

BUTTON_GHOST = ButtonStyle(
    background="transparent",
    background_hover=COLOR_GLASS_HOVER,
    text=COLOR_TEXT_SECONDARY,
    border=COLOR_GLASS_BORDER,
)

# Badge variants
BADGE_SUCCESS = BadgeStyle(
    background=COLOR_SUCCESS_DARK,
    text=COLOR_SUCCESS_LIGHT,
    glow=SHADOW_GLOW_SUCCESS,
)

BADGE_WARNING = BadgeStyle(
    background=COLOR_WARNING_DARK,
    text=COLOR_WARNING_LIGHT,
)

BADGE_ERROR = BadgeStyle(
    background=COLOR_ERROR_DARK,
    text=COLOR_ERROR_LIGHT,
    glow=SHADOW_GLOW_ERROR,
)

BADGE_INFO = BadgeStyle(
    background=COLOR_PRIMARY_DARK,
    text=COLOR_PRIMARY_LIGHT,
    glow=SHADOW_GLOW_PRIMARY,
)

BADGE_NEUTRAL = BadgeStyle(
    background=COLOR_BASE_LIGHT,
    text=COLOR_TEXT_SECONDARY,
)

BADGE_LOCKED = BadgeStyle(
    background=COLOR_LOCKED_DARK,
    text=COLOR_LOCKED_LIGHT,
    border=COLOR_LOCKED_BORDER,
)
