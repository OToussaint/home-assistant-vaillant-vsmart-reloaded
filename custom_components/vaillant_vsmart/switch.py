"""The Vaillant vSMART climate platform."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from homeassistant.components.switch import SwitchEntity, SwitchDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from vaillant_netatmo_api import ApiException, SetpointMode

from .const import DOMAIN
from .entity import VaillantCoordinator, VaillantDeviceEntity, VaillantProgramEntity

_LOGGER: logging.Logger = logging.getLogger(__package__)

homes_get_data = []

async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_devices: AddEntitiesCallback
):
    """Set up Vaillant vSMART from a config entry."""

    coordinator: VaillantCoordinator = hass.data[DOMAIN][entry.entry_id]

    new_devices = [
        VaillantScheduleSwitch(coordinator, device.id,
                               module.id, program_id=program.id)
        for device in coordinator.data.devices.values()
        for module in device.modules
        for program in module.therm_program_list
    ] + [
        VaillantHwbSwitch(coordinator, device.id)
        for device in coordinator.data.devices.values()
    ]
    async_add_devices(new_devices)


class VaillantScheduleSwitch(VaillantProgramEntity, SwitchEntity):
    """Vaillant vSMART Switch."""

    @property
    def name(self) -> str:
        """Return the name of the select."""

        return f"{self._program.name}"

    @property
    def entity_category(self) -> EntityCategory:
        """Return entity category for this switch."""

        return EntityCategory.CONFIG

    @property
    def device_class(self) -> SwitchDeviceClass:
        """Return device class for this switch."""

        return SwitchDeviceClass.SWITCH

    @property
    def is_on(self) -> bool:
        """Return true if the switch is on, false otherwise."""

        return self._program.selected

    async def async_turn_on(self, **kwargs):
        """Turn on the switch."""

        try:
            await self._client.async_switch_schedule(
                self._device_id,
                self._module_id,
                self._program_id,
            )
        except ApiException as ex:
            _LOGGER.exception(ex)

        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs):
        """Turn off the switch."""

        _LOGGER.info(
            "Active schedule can't be switched off. To change schedule, switch the one you want as active to on state."
        )


class VaillantHwbSwitch(VaillantDeviceEntity, SwitchEntity):
    """Vaillant vSMART Switch."""

    @property
    def translation_key(self):
        """Return the translation key to translate the entity's name and states."""

        return "hwb"

    @property
    def entity_category(self) -> EntityCategory:
        """Return entity category for this switch."""

        return EntityCategory.CONFIG

    @property
    def device_class(self) -> SwitchDeviceClass:
        """Return device class for this switch."""

        return SwitchDeviceClass.SWITCH

    @property
    def is_on(self) -> bool:
        """Return true if the switch is on, false otherwise."""

        return self._device.setpoint_hwb.setpoint_activate

    async def async_turn_on(self, **kwargs):
        """Turn on the switch."""
        global homes_get_data

        endtime = datetime.now() + timedelta(
            minutes=self._device.setpoint_default_duration
        )
        
        _LOGGER.debug("async_turn_on called with arguments device_id %s module_id %s",self._device_id,self._device.modules[0].id)

        if len(homes_get_data)==0:
            try:
                _LOGGER.debug("aysn_turn_off calling get_home_data") 
                homes_get_data = await self._client.async_get_home_data() 
            except ApiException as ex: 
                _LOGGER.error("Failed to fetch Vaillant home data: %s", ex) 
                return 

        if len(homes_get_data)==1:
            _HOME_ID = homes_get_data[0].home_id
        else:
            for home in homes_get_data:
                if self._device_id == home.modules[0].module_id:
                    _HOME_ID = home.home_id 
                    break

        _LOGGER.debug("calling set_state_module home_id %s device_id %s",_HOME_ID,self._device_id)

        try:
            await self._client.async_set_state_module(
                _HOME_ID,
                self._device_id,
                SetpointMode.HWB,
                True,
                setpoint_endtime=endtime,
            )
        except ApiException as ex:
            _LOGGER.exception(ex)

        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs):
        """Turn off the switch."""
        global homes_get_data

        _LOGGER.debug("async_turn_off called with arguments device_id %s module_id %s",self._device_id,self._device.modules[0].id)

        if len(homes_get_data)==0:
            try:
                _LOGGER.debug("aysn_turn_off calling get_home_data") 
                homes_get_data = await self._client.async_get_home_data() 
            except ApiException as ex: 
                _LOGGER.error("Failed to fetch Vaillant home data: %s", ex) 
                return 

        if len(homes_get_data)==1:
            _HOME_ID = homes_get_data[0].home_id
        else:
            for home in homes_get_data:
                if self._device_id == home.modules[0].module_id:
                    _HOME_ID = home.home_id 
                    break

        _LOGGER.debug("calling set_state_module home_id %s device_id %s",_HOME_ID,self._device_id)

        try:
            await self._client.async_set_state_module(
                _HOME_ID,
                self._device_id,
                SetpointMode.HWB,
                False,
            )
        except ApiException as ex:
            _LOGGER.exception(ex)
