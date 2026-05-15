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


class _IndentedDumper(yaml.SafeDumper):
    """Indent list items under their parent key.

    PyYAML's default sequence layout puts the ``-`` at the same column as
    the parent key:

        cards:
        - type: tile

    Every HA documentation example uses the indented form instead:

        cards:
          - type: tile

    Both are valid YAML, but pasting the flat form into a dashboard next
    to the user's existing cards is visually jarring. Overriding
    ``increase_indent`` with ``indentless=False`` flips PyYAML to the
    indented style without any post-processing.
    """

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:  # noqa: ARG002
        return super().increase_indent(flow, False)


def build_vacuum_dashboard_yaml(
    vacuum_entity_id: str,
    image_entity_id: str | None,
    preset_button_entity_ids: list[str],
    room_select_switch_entity_ids: list[str] | None = None,
) -> str:
    """Return a YAML string for a single-vacuum dashboard card.

    The output is a ``vertical-stack`` so one paste lands the whole device
    tile. Sections are included only when meaningful:

    - The floor-plan picture card is dropped when the vacuum has no MARD
      geometry (legacy Ayla devices, unmapped skegox devices).
    - The presets ``entities`` card is dropped when the user hasn't
      configured any cleaning presets — an empty card would render as a
      bare title with no controls.
    - The per-room "Pick rooms to clean" card is emitted when this vacuum
      has MARD-derived select switches. Pressing Start on the vacuum tile
      then dispatches a clean against the on switches (and resets them).
      Devices without MARD geometry get a navigate-to-DevTools button as
      the fallback room picker.
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

    select_switches = list(room_select_switch_entity_ids or [])
    if select_switches:
        cards.append(
            {
                "type": "entities",
                "title": "Pick rooms to clean (then press Start on the tile)",
                "show_header_toggle": False,
                "entities": list(select_switches),
            }
        )
    else:
        # Fallback for devices without per-room switches (legacy Ayla, or
        # a device whose MARD didn't parse): point to the service-call
        # form so users still have a way to pick rooms ad hoc.
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
    # ``allow_unicode=True`` lets non-ASCII characters (the ellipsis in the
    # "Pick rooms…" label, room names with accents) render as themselves
    # instead of ``…``-style escape sequences.
    return yaml.dump(
        document,
        Dumper=_IndentedDumper,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )
