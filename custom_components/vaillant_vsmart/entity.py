"""Vaillant vSMART entity classes."""
from datetime import timedelta, datetime
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from vaillant_netatmo_api import (
    ApiException,
    Device,
    Module,
    NonOkResponseException,
    Program,
    ThermostatClient,
    RequestUnauthorizedException,
    MeasurementScale,
    MeasurementItem,
    MeasurementType,
)

from .const import DOMAIN, SUPPORTED_ENERGY_MEASUREMENT_TYPES, SUPPORTED_DURATION_MEASUREMENT_TYPES

UPDATE_INTERVAL = timedelta(minutes=1)

# Measurements are daily aggregates, so they don't need the polling rate of the
# thermostat state. Refreshing them on their own schedule keeps the number of
# API calls in the range a five minute interval used to cost, 1.33 a minute
# against 1.20, while the thermostat state itself becomes five times fresher.
MEASUREMENT_UPDATE_INTERVAL = timedelta(minutes=15)

# The API aggregates measurements in daily buckets. Asking for a single day can
# return an empty result when the current day has not been aggregated yet, so a
# few days of history are requested and only the most recent bucket is used.
MEASUREMENT_LOOKBACK = timedelta(days=7)

_LOGGER: logging.Logger = logging.getLogger(__package__)


def _format_sw_version(firmware: Any) -> str | None:
    """Return the firmware version as a string, as required by the device registry."""

    return None if firmware is None else str(firmware)


class VaillantData:
    """Class holding data which coordinator provides to the entity."""

    def __init__(self, client: ThermostatClient, devices: list[Device],
                 measurements: dict[(str, str, MeasurementType), MeasurementItem]) -> None:
        """Initialize."""

        self.client = client
        self.devices = {device.id: device for device in devices}
        self.modules = {
            module.id: module for device in devices for module in device.modules
        }
        self.programs = {
            program.id: program
            for device in devices
            for module in device.modules
            for program in module.therm_program_list
        }
        self.measurements = measurements


class VaillantCoordinator(DataUpdateCoordinator[VaillantData]):
    """Class to manage fetching data from the API."""

    def __init__(self, hass: HomeAssistant, client: ThermostatClient) -> None:
        """Initialize."""

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_method=self._update_method,
            update_interval=UPDATE_INTERVAL,
        )

        self._client = client
        self._measurements = {}
        self._measurements_updated_at = None

    async def _update_method(self):
        """Fetch data from API endpoint.

        This is the place to pre-process the data to lookup tables
        so entities can quickly look up their data.
        """

        try:
            devices = await self._client.async_get_thermostats_data()

            measurements = await self._async_get_measurements(devices)

            return VaillantData(self._client, devices, measurements)
        except RequestUnauthorizedException as ex:
            raise ConfigEntryAuthFailed from ex
        except ApiException as ex:
            _LOGGER.exception(ex)
            raise UpdateFailed(f"Error communicating with API: {ex}") from ex

    async def _async_get_measurements(
        self, devices: list[Device]
    ) -> dict[(str, str, MeasurementType), list[MeasurementItem]]:
        """Return all supported measurements for all modules of all devices.

        The previously fetched measurements are reused until they reach their
        own, much longer, refresh interval.

        Not every boiler reports every measurement type, and the API answers
        with an error or an empty result for the ones it doesn't know about.
        Such a measurement is skipped instead of failing the whole update, so
        the measurements which are supported keep being updated.
        """

        now = datetime.now()

        if (
            self._measurements_updated_at is not None
            and now - self._measurements_updated_at < MEASUREMENT_UPDATE_INTERVAL
        ):
            return self._measurements

        date_begin = now - MEASUREMENT_LOOKBACK

        measurements = {}

        for device in devices:
            for module in device.modules:
                for measurement_type in (
                    SUPPORTED_ENERGY_MEASUREMENT_TYPES
                    + SUPPORTED_DURATION_MEASUREMENT_TYPES
                ):
                    try:
                        items = await self._client.async_get_measure(
                            device.id,
                            module.id,
                            measurement_type,
                            MeasurementScale.DAY,
                            date_begin,
                        )
                    except RequestUnauthorizedException:
                        raise
                    except (ApiException, NonOkResponseException) as ex:
                        _LOGGER.debug(
                            "Measurement %s is not available for module %s: %s",
                            measurement_type.value,
                            module.id,
                            ex,
                        )
                        continue

                    measurements[(device.id, module.id, measurement_type)] = items

        self._measurements = measurements
        self._measurements_updated_at = now

        return measurements


class VaillantDeviceEntity(CoordinatorEntity[VaillantData]):
    """Base class for Vaillant device entities."""

    def __init__(
        self,
        coordinator: DataUpdateCoordinator[VaillantData],
        device_id: str,
    ):
        """Initialize."""

        super().__init__(coordinator)

        self._device_id = device_id

    @property
    def _client(self) -> ThermostatClient:
        """Retrun the instance of the client which enables HTTP communication with the API."""

        return self.coordinator.data.client

    @property
    def _device(self) -> Device:
        """Return the device which this entity represents."""

        return self.coordinator.data.devices[self._device_id]

    @property
    def unique_id(self) -> str:
        """Return a unique ID to use for this entity."""

        return self._device.id

    @property
    def has_entity_name(self) -> bool:
        """Return if entity is using new entity naming conventions."""

        return True

    @property
    def device_info(self) -> dict[str, Any]:
        """Return all device info available for this entity."""

        return {
            "identifiers": {(DOMAIN, self._device.id)},
            "name": self._device.station_name,
            "sw_version": _format_sw_version(self._device.firmware),
            "manufacturer": self._device.type,
        }


class VaillantModuleEntity(CoordinatorEntity[VaillantData]):
    """Base class for Vaillant module entities."""

    def __init__(
        self,
        coordinator: DataUpdateCoordinator[VaillantData],
        device_id: str,
        module_id: str,
    ):
        """Initialize."""

        super().__init__(coordinator)

        self._device_id = device_id
        self._module_id = module_id

    @property
    def _client(self) -> ThermostatClient:
        """Retrun the instance of the client which enables HTTP communication with the API."""

        return self.coordinator.data.client

    @property
    def _device(self) -> Device:
        """Return the device which this entity represents."""

        return self.coordinator.data.devices[self._device_id]

    @property
    def _module(self) -> Module:
        """Return the module which this entity represents."""

        return self.coordinator.data.modules[self._module_id]

    @property
    def unique_id(self) -> str:
        """Return a unique ID to use for this entity."""

        return self._module.id

    @property
    def has_entity_name(self) -> bool:
        """Return if entity is using new entity naming conventions."""

        return True

    @property
    def device_info(self) -> dict[str, Any]:
        """Return all device info available for this entity."""

        return {
            "identifiers": {(DOMAIN, self._module.id)},
            "name": self._module.module_name,
            "sw_version": _format_sw_version(self._module.firmware),
            "manufacturer": self._device.type,
            "via_device": (DOMAIN, self._device.id),
        }


class VaillantProgramEntity(CoordinatorEntity[VaillantData]):
    """Base class for Vaillant program entities."""

    def __init__(
        self,
        coordinator: DataUpdateCoordinator[VaillantData],
        device_id: str,
        module_id: str,
        program_id: str,
    ):
        """Initialize."""

        super().__init__(coordinator)

        self._device_id = device_id
        self._module_id = module_id
        self._program_id = program_id

    @property
    def _client(self) -> ThermostatClient:
        """Retrun the instance of the client which enables HTTP communication with the API."""

        return self.coordinator.data.client

    @property
    def _device(self) -> Device:
        """Return the device which this entity represents."""

        return self.coordinator.data.devices[self._device_id]

    @property
    def _module(self) -> Module:
        """Return the module which this entity represents."""

        return self.coordinator.data.modules[self._module_id]

    @property
    def _program(self) -> Program:
        """Return the program which this entity represents."""

        return self.coordinator.data.programs[self._program_id]

    @property
    def unique_id(self) -> str:
        """Return a unique ID to use for this entity."""

        return self._program.id

    @property
    def has_entity_name(self) -> bool:
        """Return if entity is using new entity naming conventions."""

        return True

    @property
    def device_info(self) -> dict[str, Any]:
        """Return all device info available for this entity."""

        return {
            "identifiers": {(DOMAIN, self._module.id)},
            "name": self._module.module_name,
            "sw_version": _format_sw_version(self._module.firmware),
            "manufacturer": self._device.type,
            "via_device": (DOMAIN, self._device.id),
        }


class VaillantMeasurementEntity(CoordinatorEntity[VaillantData]):
    """Base class for Vaillant measurement entities."""

    def __init__(
        self,
        coordinator: DataUpdateCoordinator[VaillantData],
        device_id: str,
        module_id: str,
        measurement_type: MeasurementType,
    ):
        """Initialize."""

        super().__init__(coordinator)

        self._device_id = device_id
        self._module_id = module_id
        self._measurement_type = measurement_type

    @property
    def _client(self) -> ThermostatClient:
        """Retrun the instance of the client which enables HTTP communication with the API."""

        return self.coordinator.data.client

    @property
    def _device(self) -> Device:
        """Return the device which this entity represents."""

        return self.coordinator.data.devices[self._device_id]

    @property
    def _module(self) -> Module:
        """Return the module which this entity represents."""

        return self.coordinator.data.modules[self._module_id]

    @property
    def _measurement(self) -> MeasurementItem | None:
        """Return the measurement which this entity represents.

        Returns None when the API has no data for this measurement, which is
        the case for measurement types the boiler doesn't report.
        """

        items = self.coordinator.data.measurements.get(
            (self._device_id, self._module_id, self._measurement_type)
        )

        if not items or not items[-1].value:
            return None

        return items[-1]

    @property
    def available(self) -> bool:
        """Return whether the API reports data for this measurement."""

        return super().available and self._measurement is not None

    @property
    def unique_id(self) -> str:
        """Return a unique ID to use for this entity."""

        return f"{self._module.id}_{self._measurement_type}"

    @ property
    def has_entity_name(self) -> bool:
        """Return if entity is using new entity naming conventions."""

        return True

    @ property
    def device_info(self) -> dict[str, Any]:
        """Return all device info available for this entity."""

        return {
            "identifiers": {(DOMAIN, self._module.id)},
            "name": self._module.module_name,
            "sw_version": _format_sw_version(self._module.firmware),
            "manufacturer": self._device.type,
            "via_device": (DOMAIN, self._device.id),
        }
