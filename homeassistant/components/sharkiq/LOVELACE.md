# Shark IQ — Lovelace card snippets

Home Assistant's standard entity cards have no multi-select dropdown
primitive, so a "pick rooms, pick mode, pick fan speed, hit start"
form can't live on the auto-generated device card. The picker exists
in the **service-call form** for `sharkiq.clean_room` (rooms field
uses `selector.select` with `multiple: true`) — these snippets surface
that wherever it's reachable from a dashboard.

Replace `vacuum.<your_vacuum>` and `<your_vacuum>` with your actual
entity slug (typically the device's friendly name, lowercased with
underscores). Preset entity names follow `button.<your_vacuum>_clean_<preset_name>`.

---

## Quick path: let the integration generate the YAML for you

**Developer Tools → Actions → `sharkiq.dashboard_yaml`**

1. Action: `sharkiq.dashboard_yaml`
2. Target: your vacuum entity
3. **Perform action**
4. The response panel shows a `dashboard_yaml` value — copy it.
5. **Settings → Dashboards → (your dashboard) → Edit dashboard → Add card →
   Manual** → paste → Save.

This builds a card customised to your vacuum: the device tile, the
floor plan (if the device has a parseable map), every preset button
you've configured, and a "Pick rooms…" shortcut to the
`sharkiq.clean_room` form. The handcrafted snippets below remain
available if you want to compose the layout yourself.

---

## 1. One-card device controls

A single tile with start / pause / locate / return-to-dock built into
the card features, plus a list of any cleaning presets you've
configured under **Settings → Devices & Services → Shark IQ →
Configure → Cleaning presets**.

```yaml
type: vertical-stack
cards:
  - type: tile
    entity: vacuum.<your_vacuum>
    features_position: bottom
    features:
      - type: vacuum-commands
        commands:
          - start_pause
          - stop
          - locate
          - return_home
  - type: entities
    title: Cleaning presets
    show_header_toggle: false
    entities:
      - entity: button.<your_vacuum>_clean_<preset_a>
      - entity: button.<your_vacuum>_clean_<preset_b>
```

## 2. Ad-hoc multi-room cleaning

For one-off "kitchen + living room with wet mop" runs, use the
service-call form. Two ways to reach it:

**Developer Tools → Actions**
1. Action: `sharkiq.clean_room`
2. Targets: your vacuum entity
3. Fields render automatically:
   - **Rooms** — multi-select dropdown populated from the device's
     room map (the `clean_room` service description is refreshed at
     setup time with the current rooms)
   - **Clean type** — Dry / Wet / Matrix / Spot
   - **Fan speed** — Eco / Normal / Max
4. **Perform action**

**Bookmarkable URL** — pin this in your sidebar, your browser
favourites, or a `panel_iframe` view:

```
/developer-tools/action?domain=sharkiq&service=clean_room
```

This jumps straight to the form pre-filtered to the right service.

## 3. Sidebar shortcut to the picker

Add to `configuration.yaml` if you want the picker as a left-rail item:

```yaml
panel_iframe:
  shark_clean:
    title: "Shark — clean rooms"
    icon: mdi:broom
    url: /developer-tools/action?domain=sharkiq&service=clean_room
    require_admin: false
```

---

## Why not a dashboard form?

Multi-select selectors (`selector.select` with `multiple: true`) only
render in **service-call forms** in vanilla Home Assistant —
Developer Tools, the automation editor, blueprint inputs. They don't
render inside `entities`, `tile`, `button`, or any other entity-card
type. Surfacing the picker on a dashboard tile would require either
a HACS card (e.g. `custom:button-card`, `custom:service-call-card`) or
a custom Lovelace plugin, both of which are out of scope for this
integration.

If you really want one-tap room cleans on your dashboard, configure
the rooms+mode+speed combo as a **preset** under the integration's
options, then surface the resulting `button.*_clean_*` entity. The
preset system exists exactly for that.
