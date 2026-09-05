"""The device every entity of this integration belongs to.

One place, because sensors, the button and the calendar must all land on the
*same* device entry. It used to be defined three times — once per platform —
with the button and calendar docstrings noting that they mirrored the sensor's
copy, which is exactly the kind of duplication that drifts.
"""
from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN

ATTRIBUTION = "Data provided by PostNL"


def build_device_info(userinfo: dict[str, Any]) -> DeviceInfo:
    """Return DeviceInfo shared by all sensors for this account."""
    email = userinfo.get("email") or ""
    return DeviceInfo(
        identifiers={(DOMAIN, userinfo.get("account_id", ""))},
        name=f"PostNL ({email})" if email else "PostNL",
        manufacturer="PostNL",
        entry_type=DeviceEntryType.SERVICE,
        configuration_url="https://jouw.postnl.nl",
    )
