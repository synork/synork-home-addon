"""Synork Home — service\u2194Supervisor add-on mapping.

Single source of truth for which Home Assistant Supervisor add-on backs
each persona service Synork knows about. Used by the persona auto-provisioners
(:func:`HubPersona.auto_provision_ha`,
:func:`SatellitePersona.auto_provision_ha`,
:func:`SpeakerPersona.auto_provision_ha`) to install + start the matching
managed add-on before walking any HA config flow.

All slugs are stable identifiers from the official Home Assistant Core
add-on repository (always present in Supervisor installs).
"""

from __future__ import annotations

# Hub services \u2194 radio/network add-ons.
HUB_SERVICE_TO_ADDON_SLUG: dict[str, str] = {
    "zwave_js": "core_zwave_js",
    "otbr": "core_openthread_border_router",
    "matter": "core_matter_server",
}

# Satellite (voice input) services \u2194 Wyoming-protocol add-ons. When these
# add-ons start they advertise themselves over mDNS as ``_wyoming._tcp``
# and HA's ``wyoming`` integration auto-creates a config entry for each.
SATELLITE_SERVICE_TO_ADDON_SLUG: dict[str, str] = {
    "wake_word": "core_openwakeword",
    "stt": "core_whisper",  # faster-whisper Wyoming wrapper
}

# Speaker (voice output) services \u2194 Wyoming TTS add-on.
SPEAKER_SERVICE_TO_ADDON_SLUG: dict[str, str] = {
    "tts": "core_piper",
}
