"""Tests for the Shark IQ floor-plan SVG renderer."""

from __future__ import annotations

import re

import pytest

from homeassistant.components.sharkiq.floor_plan import (
    _color_for,
    render_floor_plan_svg,
)


def test_render_returns_well_formed_svg_with_one_polygon_per_area() -> None:
    """Each area contributes exactly one ``<polygon>`` element.

    Counting polygons is a tighter contract than counting bytes — it
    confirms the renderer didn't silently drop areas while still building
    a valid SVG, which is a class of bug we'd otherwise only catch by eye.
    """
    svg = render_floor_plan_svg(
        [
            {
                "display_name": "Kitchen",
                "uuid": "u1",
                "points": [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)],
            },
            {
                "display_name": "Living Room",
                "uuid": "u2",
                "points": [(10.0, 0.0), (20.0, 0.0), (20.0, 10.0), (10.0, 10.0)],
            },
        ]
    ).decode()

    assert svg.startswith("<?xml")
    assert svg.endswith("</svg>")
    assert svg.count("<polygon") == 2
    # Labels are emitted via <text>; one per area.
    assert svg.count('class="label"') == 2
    assert ">Kitchen<" in svg
    assert ">Living Room<" in svg


def test_render_auto_fits_viewbox_with_padding() -> None:
    """ViewBox covers polygon bounds plus a small padding on each side."""
    svg = render_floor_plan_svg(
        [
            {
                "display_name": "A",
                "uuid": "",
                "points": [(100.0, 200.0), (300.0, 200.0), (300.0, 500.0), (100.0, 500.0)],
            }
        ],
        padding=5.0,
    ).decode()

    match = re.search(
        r'viewBox="([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)"', svg
    )
    assert match is not None
    vb_x, vb_y, vb_w, vb_h = (float(g) for g in match.groups())
    # Padding shifts the origin and grows the bounds; widths should match
    # 200 + 2 * padding and 300 + 2 * padding within float tolerance.
    assert vb_x == pytest.approx(95.0)
    assert vb_y == pytest.approx(195.0)
    assert vb_w == pytest.approx(210.0)
    assert vb_h == pytest.approx(310.0)


def test_render_empty_areas_returns_placeholder() -> None:
    """No usable polygons still returns a valid SVG (placeholder text)."""
    svg = render_floor_plan_svg([]).decode()

    assert svg.startswith("<?xml")
    assert "<svg" in svg
    assert "<polygon" not in svg
    assert "No floor plan available" in svg


def test_render_skips_areas_without_enough_points() -> None:
    """Degenerate polygons (< 3 points) are dropped, not crashed on."""
    svg = render_floor_plan_svg(
        [
            {"display_name": "valid", "uuid": "v", "points": [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)]},
            {"display_name": "skipped", "uuid": "s", "points": [(2.0, 2.0)]},
            {"display_name": "skipped-too", "uuid": "x", "points": []},
        ]
    ).decode()

    assert svg.count("<polygon") == 1
    assert ">valid<" in svg
    assert ">skipped<" not in svg


def test_render_escapes_xml_in_room_names() -> None:
    """Room labels with XML-sensitive chars stay safe."""
    svg = render_floor_plan_svg(
        [
            {
                "display_name": "Tom & Jerry's <Den>",
                "uuid": "u",
                "points": [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)],
            }
        ]
    ).decode()

    assert "&amp;" in svg
    assert "&lt;Den&gt;" in svg
    # Original unescaped form must not appear.
    assert "<Den>" not in svg


def test_color_for_is_stable_across_calls() -> None:
    """Same key → same color, every time (hash-indexed palette pick)."""
    assert _color_for("Kitchen") == _color_for("Kitchen")
    # Different keys generally produce different colors (palette has 12 colors,
    # so collisions exist; pick two strings known to differ).
    assert _color_for("Kitchen") != _color_for("Living Room")
