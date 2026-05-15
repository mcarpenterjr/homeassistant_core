"""Tests for the Shark IQ dashboard YAML generator."""

from __future__ import annotations

import yaml

from homeassistant.components.sharkiq.dashboard_yaml import (
    build_vacuum_dashboard_yaml,
)


def test_full_output_has_tile_image_presets_and_picker() -> None:
    """All four sections render when every input is present."""
    raw = build_vacuum_dashboard_yaml(
        vacuum_entity_id="vacuum.sharknado",
        image_entity_id="image.sharknado_floor_plan",
        preset_button_entity_ids=[
            "button.sharknado_clean_kitchen",
            "button.sharknado_clean_downstairs",
        ],
    )

    doc = yaml.safe_load(raw)

    assert doc["type"] == "vertical-stack"
    types = [c["type"] for c in doc["cards"]]
    # Order matters for visual layout: device tile, then floor plan, then
    # preset list, then the pick-rooms shortcut.
    assert types == ["tile", "picture-entity", "entities", "button"]

    tile = doc["cards"][0]
    assert tile["entity"] == "vacuum.sharknado"
    assert tile["features"][0]["type"] == "vacuum-commands"

    picture = doc["cards"][1]
    assert picture["entity"] == "image.sharknado_floor_plan"

    entities_card = doc["cards"][2]
    assert entities_card["entities"] == [
        "button.sharknado_clean_kitchen",
        "button.sharknado_clean_downstairs",
    ]

    picker = doc["cards"][3]
    assert picker["tap_action"]["action"] == "navigate"
    assert "service=clean_room" in picker["tap_action"]["navigation_path"]


def test_skips_picture_card_when_no_floor_plan() -> None:
    """Legacy / unmapped devices have no image entity — drop the card.

    Without this guard the output would still render the ``picture-entity``
    card pointing at a non-existent entity, which Lovelace shows as a
    broken slot. Skipping keeps the dashboard clean.
    """
    raw = build_vacuum_dashboard_yaml(
        vacuum_entity_id="vacuum.sharknado",
        image_entity_id=None,
        preset_button_entity_ids=["button.sharknado_clean_kitchen"],
    )

    doc = yaml.safe_load(raw)
    types = [c["type"] for c in doc["cards"]]

    assert "picture-entity" not in types
    assert types == ["tile", "entities", "button"]


def test_skips_presets_card_when_none_configured() -> None:
    """Empty preset list omits the entities card entirely.

    An ``entities`` card with no entities renders as a bare title with no
    controls, which is uglier than just not rendering it.
    """
    raw = build_vacuum_dashboard_yaml(
        vacuum_entity_id="vacuum.sharknado",
        image_entity_id="image.sharknado_floor_plan",
        preset_button_entity_ids=[],
    )

    doc = yaml.safe_load(raw)
    types = [c["type"] for c in doc["cards"]]

    assert "entities" not in types
    assert types == ["tile", "picture-entity", "button"]


def test_minimal_output_has_only_tile_and_picker() -> None:
    """Worst case (no image, no presets) still produces a useful card.

    A user with a fresh device that hasn't been mapped and no presets
    configured should still get the device tile plus a one-tap shortcut
    to the room picker.
    """
    raw = build_vacuum_dashboard_yaml(
        vacuum_entity_id="vacuum.sharknado",
        image_entity_id=None,
        preset_button_entity_ids=[],
    )

    doc = yaml.safe_load(raw)
    types = [c["type"] for c in doc["cards"]]

    assert types == ["tile", "button"]


def test_output_is_block_style_yaml_with_indented_sequences() -> None:
    """The dump matches HA documentation house style: block + indented seqs.

    PyYAML's default sequence layout puts the ``-`` flush with the parent
    key column, which is valid YAML but visually unlike every example in
    HA docs. The custom dumper forces sequence items to be indented under
    their parent so the paste blends in next to existing dashboard YAML.
    """
    raw = build_vacuum_dashboard_yaml(
        vacuum_entity_id="vacuum.sharknado",
        image_entity_id="image.sharknado_floor_plan",
        preset_button_entity_ids=[],
    )

    # Block-style: no flow-style braces around the top-level document.
    assert not raw.lstrip().startswith("{")
    # Indented sequences: list items under ``cards`` start with two
    # leading spaces, not zero.
    assert "\n  - type:" in raw
    assert "\n- type:" not in raw


def test_output_emits_room_picker_when_switches_present() -> None:
    """With per-room switches the picker card replaces the navigate fallback.

    Dispatch lives on the vacuum tile's Start button (selection-aware), so
    the picker card lists only switches — no separate "clean selected"
    button entity. The header text reminds users where the dispatcher is.
    """
    raw = build_vacuum_dashboard_yaml(
        vacuum_entity_id="vacuum.sharknado",
        image_entity_id="image.sharknado_floor_plan",
        preset_button_entity_ids=[],
        room_select_switch_entity_ids=[
            "switch.sharknado_select_kitchen",
            "switch.sharknado_select_bedroom",
        ],
    )

    doc = yaml.safe_load(raw)
    types = [c["type"] for c in doc["cards"]]

    assert types == ["tile", "picture-entity", "entities"]
    picker = doc["cards"][2]
    assert "Pick rooms to clean" in picker["title"]
    assert picker["entities"] == [
        "switch.sharknado_select_kitchen",
        "switch.sharknado_select_bedroom",
    ]


def test_output_falls_back_to_navigate_button_without_switches() -> None:
    """Legacy or unmapped devices keep the navigate-to-DevTools button.

    Without switches we can't render a dashboard-native picker, so the
    only working room-pick path is the service-call form. Keeping the
    navigate button means those users still have one click to the picker.
    """
    raw = build_vacuum_dashboard_yaml(
        vacuum_entity_id="vacuum.sharknado",
        image_entity_id=None,
        preset_button_entity_ids=[],
        room_select_switch_entity_ids=[],
    )

    doc = yaml.safe_load(raw)
    types = [c["type"] for c in doc["cards"]]
    assert types == ["tile", "button"]
    assert doc["cards"][1]["tap_action"]["action"] == "navigate"


def test_output_preserves_unicode_characters() -> None:
    """Non-ASCII characters (eg the ellipsis in "Pick rooms…") render as
    themselves, not as ``\\u2026`` escape sequences.

    PyYAML defaults to ASCII-only output for safety, which results in
    literal ``\\u2026`` strings appearing in the pasted dashboard YAML.
    ``allow_unicode=True`` keeps the source characters intact.
    """
    raw = build_vacuum_dashboard_yaml(
        vacuum_entity_id="vacuum.sharknado",
        image_entity_id=None,
        preset_button_entity_ids=[],
    )

    assert "…" in raw
    assert "\\u2026" not in raw
