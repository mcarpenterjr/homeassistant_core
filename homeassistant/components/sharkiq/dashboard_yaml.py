"""Render a ready-to-paste Lovelace YAML block for a Shark IQ vacuum.

Core HA integrations can't auto-install Lovelace cards (strategies aren't
exposed to integrations, programmatic dashboard creation is private API,
and shipping JS bundles is HACS territory). Instead we generate the YAML
for a specific vacuum on demand — the user runs the ``dashboard_yaml``
action, copies the response, and pastes it into a new Lovelace card. One
manual step, with all entity IDs already filled in.
"""

from __future__ import annotations

from typing import Any

import yaml


def build_vacuum_dashboard_yaml(
    vacuum_entity_id: str,
    image_entity_id: str | None,
    preset_button_entity_ids: list[str],
) -> str:
    """Return a YAML string for a single-vacuum dashboard card.

    The output is a ``vertical-stack`` so one paste lands the whole device
    tile. Sections are included only when meaningful:

    - The floor-plan picture card is dropped when the vacuum has no MARD
      geometry (legacy Ayla devices, unmapped skegox devices).
    - The presets ``entities`` card is dropped when the user hasn't
      configured any cleaning presets — an empty card would render as a
      bare title with no controls.

    The "Pick rooms…" navigation button is always emitted: it's the
    workaround for HA's platform constraint that multi-select selectors
    only render in service-call forms, not in dashboard entity cards.
    """
    cards: list[dict[str, Any]] = [
        {
            "type": "tile",
            "entity": vacuum_entity_id,
            "features_position": "bottom",
            "features": [
                {
                    "type": "vacuum-commands",
                    "commands": ["start_pause", "stop", "locate", "return_home"],
                }
            ],
        }
    ]

    if image_entity_id is not None:
        cards.append(
            {
                "type": "picture-entity",
                "entity": image_entity_id,
                "show_state": False,
                "show_name": False,
            }
        )

    if preset_button_entity_ids:
        cards.append(
            {
                "type": "entities",
                "title": "Cleaning presets",
                "show_header_toggle": False,
                "entities": list(preset_button_entity_ids),
            }
        )

    cards.append(
        {
            "type": "button",
            "name": "Pick rooms…",
            "icon": "mdi:broom",
            "tap_action": {
                "action": "navigate",
                "navigation_path": (
                    "/developer-tools/action?domain=sharkiq&service=clean_room"
                ),
            },
        }
    )

    document: dict[str, Any] = {"type": "vertical-stack", "cards": cards}

    # ``sort_keys=False`` keeps the structure in the order a reader expects
    # (``type`` first, ``entity`` next, etc.). ``default_flow_style=False``
    # forces block-style so the paste looks like idiomatic Lovelace YAML.
    return yaml.safe_dump(
        document, sort_keys=False, default_flow_style=False
    )
