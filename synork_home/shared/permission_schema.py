"""
Synork Home — Household & Permission Schema

Defines the household model, role-based permissions, and identity context
types used across all Synork Home components.

Key design decisions:
  - Roles are fixed (OWNER, ADULT, KID, GUEST) — no custom roles in v1
  - Default permission matrix is role × device_class → PermissionLevel
  - Per-entity overrides exist for granular control
  - IdentityContext travels with every request so permission checks are
    always possible without extra lookups
  - One Synork account can belong to multiple households

Contract locked in Session 1 (2026-04-25).
Implementation: Session 2 (Backend Relay).
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class HouseholdRole(str, Enum):
    """
    Roles within a Synork Home household.

    OWNER: Created the household / paired the device. Full admin.
    ADULT: Full access, equal control to owner for day-to-day operations.
    KID: Limited access, configurable per device class. Family Mode applies.
    GUEST: Temporary, time-bounded, minimal access.
    """
    OWNER = "owner"
    ADULT = "adult"
    KID = "kid"
    GUEST = "guest"


class DeviceClass(str, Enum):
    """
    Broad device categories used for permission matrix defaults.

    Maps to HA device classes but simplified for the permission model.
    """
    LIGHT = "light"
    LOCK = "lock"
    CAMERA = "camera"
    CLIMATE = "climate"
    MEDIA = "media"
    SCENE = "scene"
    SECURITY = "security"
    SENSOR = "sensor"
    SWITCH = "switch"
    COVER = "cover"
    VACUUM = "vacuum"
    FAN = "fan"
    OTHER = "other"


class PermissionLevel(str, Enum):
    """
    Permission level for a role × device_class combination.

    FULL: Read + write + configure
    CONTROL: Read + write (toggle, set values)
    VIEW: Read only (see state, no control)
    NONE: No access (entity hidden from user)
    ASK: Must request approval from an OWNER or ADULT
    """
    FULL = "full"
    CONTROL = "control"
    VIEW = "view"
    NONE = "none"
    ASK = "ask"


class MemberStatus(str, Enum):
    """Status of a household member."""
    ACTIVE = "active"
    INVITED = "invited"
    SUSPENDED = "suspended"
    EXPIRED = "expired"


class HomeMode(str, Enum):
    """
    Predefined home modes that affect automations, notifications, and dashboards.

    Custom modes can be added by users — this enum covers built-in defaults.
    """
    HOME = "home"
    AWAY = "away"
    SLEEP = "sleep"
    MOVIE = "movie"
    GUESTS_OVER = "guests_over"
    VACATION = "vacation"
    DO_NOT_DISTURB = "dnd"


# ---------------------------------------------------------------------------
# Household model
# ---------------------------------------------------------------------------

class HouseholdMember(BaseModel):
    """
    A single member of a household.

    Links a Synork account to a household with a specific role.
    Guest members have an expires_at timestamp.
    """
    user_id: str = Field(..., description="Synork account ID.")
    display_name: str = Field(..., description="Name shown in the household UI.")
    role: HouseholdRole = Field(..., description="Role within this household.")
    status: MemberStatus = Field(default=MemberStatus.ACTIVE, description="Membership status.")
    joined_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When this member joined the household.",
    )
    invited_by: Optional[str] = Field(None, description="User ID of whoever invited this member.")
    expires_at: Optional[datetime] = Field(
        None,
        description="Expiration for GUEST members. None = no expiration.",
    )
    family_mode_enabled: bool = Field(
        default=False,
        description="Whether Family Mode is active for this member (typically KID role).",
    )
    pin_hash: Optional[str] = Field(
        None,
        description="Hashed PIN for sensitive actions. None = not set up yet.",
    )
    notification_preferences: dict[str, Any] = Field(
        default_factory=dict,
        description="Per-member notification routing preferences.",
    )


class HouseholdDevice(BaseModel):
    """A device registered to a household."""
    device_id: str = Field(..., description="Unique device identifier from pairing.")
    device_name: str = Field(..., description="Human-readable device name.")
    location: Optional[str] = Field(None, description="Room/area where device is placed.")
    paired_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When the device was paired.",
    )
    paired_by: str = Field(..., description="User ID who paired this device.")
    is_online: bool = Field(default=False, description="Whether the device is currently connected.")
    last_seen: Optional[datetime] = Field(None, description="Last heartbeat timestamp.")
    addon_version: Optional[str] = Field(None, description="Running addon version.")
    ha_version: Optional[str] = Field(None, description="Running HA Core version.")
    active_personas: list[str] = Field(
        default_factory=list,
        description="Active persona names on this device.",
    )


class Household(BaseModel):
    """
    A Synork Home household.

    Groups members and devices under a single organizational unit.
    One Synork account can be a member of multiple households.
    """
    household_id: str = Field(..., description="Unique household identifier.")
    name: str = Field(..., description="Household name (e.g. 'Kovács család').")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When the household was created.",
    )
    owner_user_id: str = Field(..., description="Synork user ID of the household owner.")
    members: list[HouseholdMember] = Field(
        default_factory=list,
        description="All household members.",
    )
    devices: list[HouseholdDevice] = Field(
        default_factory=list,
        description="All devices registered to this household.",
    )
    active_modes: list[HomeMode] = Field(
        default_factory=list,
        description="Currently active home modes.",
    )
    timezone: str = Field(
        default="Europe/Budapest",
        description="Household timezone (IANA format).",
    )
    language: str = Field(
        default="hu",
        description="Primary language for this household.",
    )
    settings: dict[str, Any] = Field(
        default_factory=dict,
        description="Household-level settings (notification defaults, etc.).",
    )


# ---------------------------------------------------------------------------
# Permission matrix
# ---------------------------------------------------------------------------

class PermissionRule(BaseModel):
    """A single rule in the permission matrix: role × device_class → level."""
    role: HouseholdRole = Field(..., description="Which role this rule applies to.")
    device_class: DeviceClass = Field(..., description="Which device class this rule covers.")
    level: PermissionLevel = Field(..., description="Permission level granted.")


class EntityPermission(BaseModel):
    """
    Per-entity permission override.

    Overrides the default permission matrix for a specific entity and user/role.
    """
    entity_id: str = Field(..., description="HA entity ID this override applies to.")
    user_id: Optional[str] = Field(
        None,
        description="Specific user this override applies to. None = applies to role.",
    )
    role: Optional[HouseholdRole] = Field(
        None,
        description="Role this override applies to. None = applies to specific user.",
    )
    level: PermissionLevel = Field(..., description="Overridden permission level.")
    reason: Optional[str] = Field(None, description="Human-readable reason for the override.")
    set_by: str = Field(..., description="User ID who created this override.")
    set_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When the override was created.",
    )


class PermissionMatrix(BaseModel):
    """
    Complete permission matrix for a household.

    Combines default role-based rules with per-entity overrides.

    Resolution order:
      1. Check EntityPermission for (entity_id, user_id) — most specific
      2. Check EntityPermission for (entity_id, role) — role-specific entity override
      3. Check PermissionRule for (role, device_class) — default matrix
      4. Fall back to PermissionLevel.NONE
    """
    household_id: str = Field(..., description="Household this matrix belongs to.")
    default_rules: list[PermissionRule] = Field(
        default_factory=list,
        description="Default role × device_class rules.",
    )
    entity_overrides: list[EntityPermission] = Field(
        default_factory=list,
        description="Per-entity overrides.",
    )

    @staticmethod
    def sensible_defaults() -> list[PermissionRule]:
        """
        Generate the default permission matrix.

        Owner and Adult get FULL on everything.
        Kid gets CONTROL on lights/media/scene/fan, VIEW on sensors, NONE on locks/cameras/security.
        Guest gets CONTROL on lights/media, VIEW on climate/sensors, NONE on everything else.
        """
        rules: list[PermissionRule] = []

        for device_class in DeviceClass:
            # Owner: full access everywhere
            rules.append(PermissionRule(role=HouseholdRole.OWNER, device_class=device_class, level=PermissionLevel.FULL))
            # Adult: full access everywhere
            rules.append(PermissionRule(role=HouseholdRole.ADULT, device_class=device_class, level=PermissionLevel.FULL))

        # Kid defaults
        kid_permissions = {
            DeviceClass.LIGHT: PermissionLevel.CONTROL,
            DeviceClass.LOCK: PermissionLevel.NONE,
            DeviceClass.CAMERA: PermissionLevel.NONE,
            DeviceClass.CLIMATE: PermissionLevel.VIEW,
            DeviceClass.MEDIA: PermissionLevel.CONTROL,
            DeviceClass.SCENE: PermissionLevel.CONTROL,
            DeviceClass.SECURITY: PermissionLevel.NONE,
            DeviceClass.SENSOR: PermissionLevel.VIEW,
            DeviceClass.SWITCH: PermissionLevel.CONTROL,
            DeviceClass.COVER: PermissionLevel.ASK,
            DeviceClass.VACUUM: PermissionLevel.CONTROL,
            DeviceClass.FAN: PermissionLevel.CONTROL,
            DeviceClass.OTHER: PermissionLevel.VIEW,
        }
        for device_class, level in kid_permissions.items():
            rules.append(PermissionRule(role=HouseholdRole.KID, device_class=device_class, level=level))

        # Guest defaults
        guest_permissions = {
            DeviceClass.LIGHT: PermissionLevel.CONTROL,
            DeviceClass.LOCK: PermissionLevel.NONE,
            DeviceClass.CAMERA: PermissionLevel.NONE,
            DeviceClass.CLIMATE: PermissionLevel.VIEW,
            DeviceClass.MEDIA: PermissionLevel.CONTROL,
            DeviceClass.SCENE: PermissionLevel.NONE,
            DeviceClass.SECURITY: PermissionLevel.NONE,
            DeviceClass.SENSOR: PermissionLevel.VIEW,
            DeviceClass.SWITCH: PermissionLevel.NONE,
            DeviceClass.COVER: PermissionLevel.NONE,
            DeviceClass.VACUUM: PermissionLevel.NONE,
            DeviceClass.FAN: PermissionLevel.CONTROL,
            DeviceClass.OTHER: PermissionLevel.NONE,
        }
        for device_class, level in guest_permissions.items():
            rules.append(PermissionRule(role=HouseholdRole.GUEST, device_class=device_class, level=level))

        return rules


# ---------------------------------------------------------------------------
# Identity context — travels with every request
# ---------------------------------------------------------------------------

class IdentityContext(BaseModel):
    """
    Identity context attached to every Synork Home operation.

    This is the "who is asking, in what context" object that every
    service and router uses for permission decisions. Built by the
    auth middleware from the session token.
    """
    asking_user_id: str = Field(..., description="Synork user ID making the request.")
    asking_user_role: HouseholdRole = Field(..., description="Role of the asking user in this household.")
    household_id: str = Field(..., description="Household context for this request.")
    subject_user_id: Optional[str] = Field(
        None,
        description=(
            "If the request concerns another user's data (e.g. 'Where is Alex?'), "
            "this is that user's ID. Permission checks consider both asking and subject."
        ),
    )
    device_id: Optional[str] = Field(
        None,
        description="Device the request originated from (if from a Synork Home device).",
    )
    session_token: Optional[str] = Field(
        None,
        description="Session token for relay auth.",
    )
    request_scope: list[str] = Field(
        default_factory=list,
        description="Scopes this request is authorized for (e.g. ['home.control', 'home.view']).",
    )
    family_mode: bool = Field(
        default=False,
        description="Whether Family Mode is active for the asking user.",
    )
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When this context was constructed.",
    )
