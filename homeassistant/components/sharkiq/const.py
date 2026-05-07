"""Shark IQ Constants."""

from datetime import timedelta
import logging

from homeassistant.const import Platform

LOGGER = logging.getLogger(__package__)

API_TIMEOUT = 20
PLATFORMS = [Platform.BUTTON, Platform.VACUUM]
DOMAIN = "sharkiq"
SHARK = "Shark"
# Default poll cadence used while the vacuum is actively cleaning or returning.
UPDATE_INTERVAL = timedelta(seconds=30)
# Slower cadence used while the vacuum is idle/docked — most of the time —
# to reduce load on the SharkNinja API and on hosts running HA.
UPDATE_INTERVAL_IDLE = timedelta(minutes=15)
# How long an entity stays "available" after the last successful coordinator
# refresh, even if subsequent polls fail. This prevents the entity from
# flapping unavailable on transient SharkNinja / network hiccups, which are
# common for an API that doesn't expose a push channel.
AVAILABILITY_GRACE = timedelta(minutes=15)

ATTR_ROOMS = "rooms"
ATTR_CLEAN_TYPE = "clean_type"
ATTR_FAN_SPEED = "fan_speed"

# Valid clean_type values for the skegox AreasToClean_V3 payload.
# Mirrors the modes the SharkClean app exposes for newer hybrid models.
# Some devices may not support all of them; the app surfaces only what
# the device's hardware can do (e.g. "wet" needs the mop pad attached).
CLEAN_TYPES = ("dry", "wet", "matrix", "spot")

# Display names accepted in the clean_room service's fan_speed field.
# Matches FAN_SPEEDS_MAP in vacuum.py exactly so the entity can map them
# back to the sharkiq library's PowerModes enum.
FAN_SPEED_NAMES = ("Eco", "Normal", "Max")

SHARKIQ_REGION_EUROPE = "europe"
SHARKIQ_REGION_ELSEWHERE = "elsewhere"
SHARKIQ_REGION_DEFAULT = SHARKIQ_REGION_ELSEWHERE
SHARKIQ_REGION_OPTIONS = [SHARKIQ_REGION_EUROPE, SHARKIQ_REGION_ELSEWHERE]

# Backend types
BACKEND_AYLA = "ayla"
BACKEND_SKEGOX = "skegox"
CONF_BACKEND = "backend"

# Options flow: cleaning presets. Each preset bundles a name, the target
# vacuum's serial number, the rooms to clean, the clean mode, and the fan
# speed. Each preset materializes as one button entity per device card.
CONF_PRESETS = "presets"
PRESET_ID = "id"
PRESET_NAME = "name"
PRESET_SERIAL = "serial"
PRESET_ROOMS = "rooms"
PRESET_CLEAN_TYPE = "clean_type"
PRESET_FAN_SPEED = "fan_speed"

# Auth0 settings.
#
# These are the SharkClean iOS app's public OAuth client credentials, which
# is what the integration's PKCE flow targets — using them means Auth0
# treats our requests as legitimate app traffic and runs any verification
# challenges interactively in the user's browser, rather than rejecting
# server-initiated logins outright with `requires_verification`.
AUTH0_CLIENT_ID_US = "wsguxrqm77mq4LtrTrwg8ZJUxmSrexGi"
AUTH0_CLIENT_ID_EU = "rKDx9O18dBrY3eoJMTkRiBZHDvd9Mx1I"
AUTH0_TOKEN_URL_US = "https://login.sharkninja.com/oauth/token"
AUTH0_TOKEN_URL_EU = "https://logineu.sharkninja.com/oauth/token"
AUTH0_AUTHORIZE_URL_US = "https://login.sharkninja.com/authorize"
AUTH0_AUTHORIZE_URL_EU = "https://logineu.sharkninja.com/authorize"
# Custom URI scheme registered by the SharkClean iOS app. Auth0 redirects
# here with the authorization code; desktop browsers fail to launch the
# scheme but still surface the URL with the ``code`` query param, which
# the user copies and pastes back into the config flow.
SHARKCLEAN_REDIRECT_URI = (
    "com.sharkninja.shark://login.sharkninja.com/ios/com.sharkninja.shark/callback"
)
AUTH0_SCOPES = "openid email profile offline_access"
# Stored token fields on ``config_entry.data``. We keep the refresh token
# and id_token from a successful PKCE exchange; the password is never
# stored or asked for.
CONF_REFRESH_TOKEN = "refresh_token"
CONF_ID_TOKEN = "id_token"
CONF_TOKEN_EXPIRY = "token_expiry"

# Skegox API settings
SKEGOX_BASE_URL_US = "https://stakra.slatra.thor.skegox.com"
SKEGOX_BASE_URL_EU = "https://stakra.rannsaka.thor.skegox.com"
SKEGOX_API_KEY_US = "QQdbSrgicK2PxvACI1a2P5AN2xgO78Lw1VvnYczb"
SKEGOX_API_KEY_EU = "T5m8d45crZDV9I5aCEZr4n2gSqJW64r2RNXqqhh1"
SKEGOX_CALLER = "ENDUSER_MOBILEAPP"
