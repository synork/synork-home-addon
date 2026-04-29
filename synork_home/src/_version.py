"""Synork Home addon version constant.

Single source of truth for the addon version, mirrored to ``config.yaml``
at deploy time. Imported by relay_client (sent in AddonHello), the wizard
(rendered in the footer), and HA registration/sensor publishing.

Bump this string AND ``config.yaml`` together when releasing.
"""

ADDON_VERSION = "0.5.6"
