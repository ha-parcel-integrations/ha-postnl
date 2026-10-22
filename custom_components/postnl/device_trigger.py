"""Device triggers for the PostNL integration.

Surfaces the parcel (and letter) events the coordinator fires on the HA
event bus as no-code automation triggers, so users can pick "a parcel was
registered" from the automation UI instead of hand-writing an event
trigger. Each trigger filters on the account's ``device_id`` (attached to
every event) so multi-account setups only fire for the selected device.
"""

from __future__ import annotations

from homeassistant.components.device_automation import DEVICE_TRIGGER_BASE_SCHEMA
from homeassistant.components.homeassistant.triggers import event as event_trigger
from homeassistant.const import CONF_DEVICE_ID, CONF_DOMAIN, CONF_PLATFORM, CONF_TYPE
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers.trigger import TriggerActionType, TriggerInfo
from homeassistant.helpers.typing import ConfigType
import voluptuous as vol

from .const import DOMAIN

# Device-trigger type -> bus event fired by the coordinator. The first three
# mirror DHL and DPD; ``letter_announced`` is PostNL-specific (mailbox post).
TRIGGER_EVENTS = {
    "parcel_registered": f"{DOMAIN}_parcel_registered",
    "parcel_status_changed": f"{DOMAIN}_parcel_status_changed",
    "parcel_delivered": f"{DOMAIN}_parcel_delivered",
    "parcel_delivery_time_changed": f"{DOMAIN}_parcel_delivery_time_changed",
    "outgoing_parcel_status_changed": f"{DOMAIN}_outgoing_parcel_status_changed",
    "outgoing_parcel_delivered": f"{DOMAIN}_outgoing_parcel_delivered",
    "letter_announced": f"{DOMAIN}_letter_announced",
}
TRIGGER_TYPES = set(TRIGGER_EVENTS)

TRIGGER_SCHEMA = DEVICE_TRIGGER_BASE_SCHEMA.extend(
    {vol.Required(CONF_TYPE): vol.In(TRIGGER_TYPES)}
)


async def async_get_triggers(
    hass: HomeAssistant, device_id: str
) -> list[dict[str, str]]:
    """Return the list of parcel/letter triggers for a PostNL device."""
    return [
        {
            CONF_PLATFORM: "device",
            CONF_DOMAIN: DOMAIN,
            CONF_DEVICE_ID: device_id,
            CONF_TYPE: trigger_type,
        }
        for trigger_type in TRIGGER_TYPES
    ]


async def async_attach_trigger(
    hass: HomeAssistant,
    config: ConfigType,
    action: TriggerActionType,
    trigger_info: TriggerInfo,
) -> CALLBACK_TYPE:
    """Attach a device trigger by delegating to the event trigger.

    The event trigger filters on the ``device_id`` carried in each event,
    so the automation only fires for the selected account.
    """
    event_config = event_trigger.TRIGGER_SCHEMA(
        {
            event_trigger.CONF_PLATFORM: "event",
            event_trigger.CONF_EVENT_TYPE: TRIGGER_EVENTS[config[CONF_TYPE]],
            event_trigger.CONF_EVENT_DATA: {CONF_DEVICE_ID: config[CONF_DEVICE_ID]},
        }
    )
    return await event_trigger.async_attach_trigger(
        hass, event_config, action, trigger_info, platform_type="device"
    )
