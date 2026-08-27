"""The Vaillant vSMART climate platform."""
from __future__ import annotations

from datetime import datetime, timedelta
import json
import logging

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    HVACAction,
    HVACMode,
    ClimateEntityFeature,
    PRESET_AWAY,
    PRESET_NONE,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from vaillant_netatmo_api import (
    ApiException,
    NonOkResponseException,
    SetpointMode,
    SystemMode,
)

from .const import (
    DOMAIN,
)
from .entity import VaillantCoordinator, VaillantModuleEntity

_LOGGER = logging.getLogger(__name__)

DEFAULT_TEMPERATURE_INCREASE = 1

SUPPORTED_FEATURES = (
    ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.PRESET_MODE
)
SUPPORTED_HVAC_MODES = [HVACMode.AUTO, HVACMode.HEAT]
SUPPORTED_PRESET_MODES = [PRESET_NONE, PRESET_AWAY]

# Netatmo error code returned when the thermostat refuses an operation. The API
# answers it with a 403, which the API client reports as an expired access
# token.
FORBIDDEN_OPERATION_ERROR = 13

# Endpoint of the room based API, the one the thermostat still accepts setpoint
# changes on. The API client hardcodes the setpoint mode of its own room call to
# "manual", so returning a room to its schedule has to be posted here directly.
SET_STATE_PATH = "syncapi/v1/setstate"

# Endpoint of the home wide modes, away among them.
SET_THERM_MODE_PATH = "api/setthermmode"

# Setpoint mode which makes a room follow its schedule again. Same value as the
# one the Netatmo integration of Home Assistant uses to leave a manual boost.
SETPOINT_MODE_SCHEDULE = "home"

# Home modes. Beware of the asymmetry with the room setpoint above: following
# the schedule is "home" for a room but "schedule" for the home.
HOME_MODE_AWAY = "away"
HOME_MODE_SCHEDULE = "schedule"

RESPONSE_STATUS_OK = "ok"


def _netatmo_error_code(ex: ApiException) -> int | None:
    """Return the error code carried by an API error response, if any."""

    response = getattr(ex, "response", None)

    if not response:
        return None

    try:
        return json.loads(response["body"])["error"]["code"]
    except (KeyError, TypeError, ValueError):
        return None


def _api_error(action: str, ex: ApiException) -> HomeAssistantError:
    """Turn an API exception into an error which can be shown to the user."""

    if _netatmo_error_code(ex) == FORBIDDEN_OPERATION_ERROR:
        return HomeAssistantError(
            f"Vaillant refused to {action}. The thermostat does not accept this "
            "change in its current system mode."
        )

    return HomeAssistantError(f"Error while trying to {action}: {ex}")


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_devices: AddEntitiesCallback
):
    """Set up Vaillant vSMART from a config entry."""

    coordinator: VaillantCoordinator = hass.data[DOMAIN][entry.entry_id]

    new_devices = [
        VaillantClimate(coordinator, device.id, module.id)
        for device in coordinator.data.devices.values()
        for module in device.modules
    ]
    async_add_devices(new_devices)


class VaillantClimate(VaillantModuleEntity, ClimateEntity):
    """Vaillant vSMART Climate."""

    def __init__(self, coordinator, device_id: str, module_id: str) -> None:
        """Initialize."""

        super().__init__(coordinator, device_id, module_id)

        self._home_id = None
        self._room_id = None


    @property
    def name(self) -> str:
        """Return the name of the climate."""

        return None

    @property
    def supported_features(self) -> int:
        """Return the flag of supported features for the climate."""

        return SUPPORTED_FEATURES

    @property
    def temperature_unit(self) -> str:
        """Return the measurement unit for all temperature values."""

        return UnitOfTemperature.CELSIUS

    @property
    def current_temperature(self) -> float:
        """Return the current room temperature."""

        return self._module.measured.temperature

    @property
    def target_temperature(self) -> float:
        """Return the targeted room temperature."""

        return (
            self._module.measured.est_setpoint_temp
            if self._module.measured.est_setpoint_temp is not None
            else self._module.measured.setpoint_temp
        )

    @property
    def hvac_action(self) -> HVACAction:
        """Return the currently running HVAC action."""

        if self._device.system_mode in [SystemMode.FROSTGUARD, SystemMode.SUMMER]:
            return HVACAction.OFF

        if self._module.boiler_status is True:
            return HVACAction.HEATING

        return HVACAction.IDLE

    @property
    def hvac_modes(self) -> list[HVACMode]:
        """Return the list of available HVAC operation modes."""

        return SUPPORTED_HVAC_MODES

    @property
    def hvac_mode(self) -> HVACMode:
        """Return currently selected HVAC operation mode.

        Only setpoints placed through the legacy endpoint show up here. A
        setpoint placed on the room, which is what setting a temperature does,
        is not part of the polled module data, so a manual setpoint can go
        unnoticed. Reading it back would need the home status endpoint, which
        the API client does not implement.
        """

        if self._module.setpoint_manual.setpoint_activate:
            return HVACMode.HEAT

        return HVACMode.AUTO

    @property
    def preset_modes(self) -> list[str]:
        """Return the list of available HVAC preset modes."""

        return SUPPORTED_PRESET_MODES

    @property
    def preset_mode(self) -> str:
        """Return the currently selected HVAC preset mode.

        Away set on the home is no more visible here than a manual setpoint set
        on the room, for the same reason.
        """

        if self._module.setpoint_away.setpoint_activate:
            return PRESET_AWAY

        return PRESET_NONE

    async def _async_home_and_room_ids(self) -> tuple[str, str]:
        """Return the home and room this thermostat belongs to."""

        if self._home_id is not None:
            return self._home_id, self._room_id

        try:
            homes = await self._client.async_get_home_data()
        except ApiException as ex:
            raise _api_error("fetch the home data", ex) from ex
        except NonOkResponseException as ex:
            raise HomeAssistantError(f"Error while fetching the home data: {ex}") from ex

        for home in homes:
            for room in home.rooms:
                if (
                    room.room_module_id == self._module_id
                    or room.room_device_id == self._device_id
                ):
                    self._home_id = home.home_id
                    self._room_id = room.room_id

                    return self._home_id, self._room_id

        # A single home with a room which carries no module or relay id still
        # leaves no doubt about which room is meant.
        if len(homes) == 1 and homes[0].rooms:
            self._home_id = homes[0].home_id
            self._room_id = homes[0].rooms[0].room_id

            return self._home_id, self._room_id

        raise HomeAssistantError(
            f"No room of the Vaillant home data matches module {self._module_id}"
        )

    async def _async_set_room_schedule(self) -> None:
        """Make the room follow its schedule again.

        Posted directly because the room call of the API client always sends a
        manual setpoint, and the legacy endpoint which could cancel one is
        refused by the thermostat with "Operation is forbidden".
        """

        home_id, room_id = await self._async_home_and_room_ids()

        body = await self._client._post(
            SET_STATE_PATH,
            json={
                "home": {
                    "id": home_id,
                    "rooms": [
                        {
                            "id": room_id,
                            "therm_setpoint_mode": SETPOINT_MODE_SCHEDULE,
                        }
                    ],
                }
            },
        )

        if body.get("status") != RESPONSE_STATUS_OK:
            raise HomeAssistantError(
                f"Vaillant refused to switch to auto mode: {body}"
            )

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Select new HVAC operation mode."""

        _LOGGER.debug("Setting HVAC mode to: %s", hvac_mode)

        if hvac_mode == HVACMode.HEAT:
            new_temperature = (
                self._module.measured.temperature + DEFAULT_TEMPERATURE_INCREASE
            )

            await self._async_set_room_temperature(new_temperature)
        elif hvac_mode == HVACMode.AUTO:
            try:
                await self._async_set_room_schedule()
            except ApiException as ex:
                raise _api_error("switch to auto mode", ex) from ex

        await self.coordinator.async_request_refresh()

    async def _async_set_home_mode(self, mode: str, action: str) -> None:
        """Set the mode of the whole home.

        Away is a mode of the home, not a setpoint of the room, so it goes to
        its own endpoint. The API client has no method for it, so the call is
        posted directly, same as the room schedule one.
        """

        home_id, _ = await self._async_home_and_room_ids()

        try:
            body = await self._client._post(
                SET_THERM_MODE_PATH,
                data={"home_id": home_id, "mode": mode},
            )
        except ApiException as ex:
            raise _api_error(action, ex) from ex

        if body.get("status") != RESPONSE_STATUS_OK:
            raise HomeAssistantError(f"Vaillant refused to {action}: {body}")

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Select new HVAC preset mode."""

        _LOGGER.debug("Setting HVAC preset mode to: %s", preset_mode)

        if preset_mode == PRESET_AWAY:
            await self._async_set_home_mode(HOME_MODE_AWAY, "switch to away mode")
        elif preset_mode == PRESET_NONE:
            await self._async_set_home_mode(HOME_MODE_SCHEDULE, "cancel away mode")
        else:
            raise HomeAssistantError(f"Unsupported preset mode: {preset_mode}")

        await self.coordinator.async_request_refresh()

    async def _async_set_room_temperature(self, new_temperature: float) -> None:
        """Place a manual setpoint on the room of this thermostat."""

        home_id, room_id = await self._async_home_and_room_ids()

        endtime = datetime.now() + timedelta(
            minutes=self._device.setpoint_default_duration
        )

        _LOGGER.debug(
            "Setting target temperature to %s for home %s room %s",
            new_temperature,
            home_id,
            room_id,
        )

        try:
            await self._client.async_set_state_room(
                home_id,
                room_id,
                SetpointMode.MANUAL,
                True,
                setpoint_endtime=endtime,
                setpoint_temp=new_temperature,
            )
        except ApiException as ex:
            raise _api_error("set the target temperature", ex) from ex
        except NonOkResponseException as ex:
            raise HomeAssistantError(
                f"Vaillant refused to set the target temperature: {ex}"
            ) from ex

    async def async_set_temperature(self, **kwargs) -> None:
        """Update target room temperature value."""

        new_temperature = kwargs.get(ATTR_TEMPERATURE)
        if new_temperature is None:
            return

        await self._async_set_room_temperature(new_temperature)

        await self.coordinator.async_request_refresh()
