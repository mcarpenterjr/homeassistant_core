"""Render Shark IQ floor plans as SVG from MARD polygon data.

Pure-Python renderer with no external image-library dependency. The output
is text/SVG which Home Assistant's image platform serves directly to the
frontend — vector means it scales to any dashboard tile without resampling
artifacts, and the file is small enough (~few KB for a typical home) to
return on every entity request without caching.
"""

from __future__ import annotations

from collections.abc import Iterable
import hashlib
from typing import TypedDict

# Distinct, dashboard-friendly colors. Hash-indexed per room so a given
# room keeps the same color across HA restarts even though the MARD
# ordering can change. Chosen for adequate contrast against both light
# and dark Lovelace themes; the fill is rendered at low opacity so labels
# stay legible.
_PALETTE: tuple[str, ...] = (
    "#1f77b4",  # blue
    "#ff7f0e",  # orange
    "#2ca02c",  # green
    "#d62728",  # red
    "#9467bd",  # purple
    "#8c564b",  # brown
    "#e377c2",  # pink
    "#17becf",  # cyan
    "#bcbd22",  # olive
    "#7f7f7f",  # grey
    "#f7b6d2",  # light pink
    "#aec7e8",  # light blue
)


class Area(TypedDict):
    """Geometry payload for a single room polygon."""

    display_name: str
    points: list[tuple[float, float]]
    uuid: str


def _color_for(key: str) -> str:
    """Deterministic palette pick keyed on room name (or uuid as fallback)."""
    if not key:
        return _PALETTE[0]
    h = int(hashlib.sha256(key.encode("utf-8")).hexdigest(), 16)
    return _PALETTE[h % len(_PALETTE)]


def _centroid(points: list[tuple[float, float]]) -> tuple[float, float]:
    """Average vertex position — close enough for label placement.

    The geometric centroid of a non-convex polygon can land outside the
    shape, but room polygons from MARD are simple enough (axis-aligned
    rectangles with the occasional notch) that the arithmetic mean is
    reliably inside. Avoiding the shoelace formula keeps this dependency-
    free and trivially fast on the dozen-or-so points per room.
    """
    n = len(points)
    return (sum(x for x, _ in points) / n, sum(y for _, y in points) / n)


def render_floor_plan_svg(
    areas: Iterable[Area],
    *,
    title: str | None = None,
    width: int = 600,
    padding: float = 8.0,
) -> bytes:
    """Render the given room polygons as an SVG byte string.

    Auto-fits the viewBox to the polygon bounds with a small padding so the
    map doesn't kiss the edge of the dashboard tile. Polygons render with a
    translucent fill plus a solid outline; room names go at the polygon
    centroid in a size proportional to the map's smaller dimension so the
    text doesn't dwarf a tiny room or vanish in a large one.

    Coordinates are taken verbatim from MARD. The device's Y axis appears
    to match the SharkClean app's top-down view, so no axis flip is
    needed; if a particular device disagrees, surface that as a config
    option rather than hard-coding a flip here.
    """
    area_list = [a for a in areas if a.get("points")]
    if not area_list:
        return _empty_svg(width, title)

    all_points = [pt for a in area_list for pt in a["points"]]
    min_x = min(x for x, _ in all_points) - padding
    max_x = max(x for x, _ in all_points) + padding
    min_y = min(y for _, y in all_points) - padding
    max_y = max(y for _, y in all_points) + padding
    vb_w = max_x - min_x
    vb_h = max_y - min_y
    if vb_w <= 0 or vb_h <= 0:
        return _empty_svg(width, title)

    height = int(round(width * vb_h / vb_w))
    # Font size scales with the map's smaller dimension. The /22 divisor was
    # eyeballed against the sample MARD body (a 12-room home rendering at
    # 600x600) to land labels at ~12pt on the dashboard.
    font_size = max(1.5, min(vb_w, vb_h) / 22)
    stroke_width = max(0.4, min(vb_w, vb_h) / 200)

    parts: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="{width}" height="{height}" '
            f'viewBox="{min_x:.3f} {min_y:.3f} {vb_w:.3f} {vb_h:.3f}" '
            f'preserveAspectRatio="xMidYMid meet">'
        ),
        (
            '<style>'
            '.room{fill-opacity:.35;stroke-linejoin:round}'
            '.label{font-family:sans-serif;text-anchor:middle;'
            'dominant-baseline:central;fill:#222;paint-order:stroke;'
            'stroke:#fff;stroke-width:0.25;stroke-linejoin:round}'
            '</style>'
        ),
    ]
    if title:
        parts.append(f'<title>{_xml_escape(title)}</title>')

    for area in area_list:
        color = _color_for(area.get("uuid") or area["display_name"])
        pts = " ".join(f"{x:.3f},{y:.3f}" for x, y in area["points"])
        parts.append(
            f'<polygon class="room" points="{pts}" '
            f'fill="{color}" stroke="{color}" stroke-width="{stroke_width:.3f}"/>'
        )
        cx, cy = _centroid(area["points"])
        parts.append(
            f'<text class="label" x="{cx:.3f}" y="{cy:.3f}" '
            f'font-size="{font_size:.2f}">{_xml_escape(area["display_name"])}</text>'
        )

    parts.append("</svg>")
    return "".join(parts).encode("utf-8")


def _empty_svg(width: int, title: str | None) -> bytes:
    """A small placeholder shown when no usable polygons are available."""
    msg = title or "No floor plan available"
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{width}" height="{width}" viewBox="0 0 100 100">'
        f'<rect width="100" height="100" fill="#eee"/>'
        f'<text x="50" y="50" font-family="sans-serif" font-size="6" '
        f'text-anchor="middle" dominant-baseline="central" fill="#666">'
        f'{_xml_escape(msg)}</text>'
        f'</svg>'
    ).encode("utf-8")


def _xml_escape(text: str) -> str:
    """Minimal XML escaping for room names and titles."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
