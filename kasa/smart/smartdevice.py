"""Module for a SMART device."""

from __future__ import annotations

import base64
import logging
import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any, cast

from ..aestransport import AesTransport
from ..device import Device, WifiNetwork
from ..device_type import DeviceType
from ..deviceconfig import DeviceConfig
from ..exceptions import AuthenticationError, DeviceError, KasaException, SmartErrorCode
from ..feature import Feature
from ..module import Module
from ..modulemapping import ModuleMapping, ModuleName
from ..smartprotocol import SmartProtocol
from .modules import (
    ChildDevice,
    Cloud,
    DeviceModule,
    Firmware,
    Light,
    Time,
)
from .smartmodule import SmartModule

_LOGGER = logging.getLogger(__name__)


# List of modules that non hub devices with children, i.e. ks240/P300, report on
# the child but only work on the parent.  See longer note below in _initialize_modules.
# This list should be updated when creating new modules that could have the
# same issue, homekit perhaps?
NON_HUB_PARENT_ONLY_MODULES = [DeviceModule, Time, Firmware, Cloud]

# Modules that are called as part of the init procedure on first update
FIRST_UPDATE_MODULES = {DeviceModule, ChildDevice, Cloud}


# Device must go last as the other interfaces also inherit Device
# and python needs a consistent method resolution order.
class SmartDevice(Device):
    """Base class to represent a SMART protocol based device."""

    def __init__(
        self,
        host: str,
        *,
        config: DeviceConfig | None = None,
        protocol: SmartProtocol | None = None,
    ) -> None:
        _protocol = protocol or SmartProtocol(
            transport=AesTransport(config=config or DeviceConfig(host=host)),
        )
        super().__init__(host=host, config=config, protocol=_protocol)
        self.protocol: SmartProtocol
        self._components_raw: dict[str, Any] | None = None
        self._components: dict[str, int] = {}
        self._state_information: dict[str, Any] = {}
        self._modules: dict[str | ModuleName[Module], SmartModule] = {}
        self._parent: SmartDevice | None = None
        self._children: Mapping[str, SmartDevice] = {}
        self._last_update = {}
        self._last_update_time: float | None = None

    async def _initialize_children(self):
        """Initialize children for power strips."""
        child_info_query = {
            "get_child_device_component_list": None,
            "get_child_device_list": None,
        }
        resp = await self.protocol.query(child_info_query)
        self.internal_state.update(resp)

        children = self.internal_state["get_child_device_list"]["child_device_list"]
        children_components = {
            child["device_id"]: {
                comp["id"]: int(comp["ver_code"]) for comp in child["component_list"]
            }
            for child in self.internal_state["get_child_device_component_list"][
                "child_component_list"
            ]
        }
        from .smartchilddevice import SmartChildDevice

        self._children = {
            child_info["device_id"]: await SmartChildDevice.create(
                parent=self,
                child_info=child_info,
                child_components=children_components[child_info["device_id"]],
            )
            for child_info in children
        }

    @property
    def children(self) -> Sequence[SmartDevice]:
        """Return list of children."""
        return list(self._children.values())

    @property
    def modules(self) -> ModuleMapping[SmartModule]:
        """Return the device modules."""
        return cast(ModuleMapping[SmartModule], self._modules)

    def _try_get_response(self, responses: dict, request: str, default=None) -> dict:
        response = responses.get(request)
        if isinstance(response, SmartErrorCode):
            _LOGGER.debug(
                "Error %s getting request %s for device %s",
                response,
                request,
                self.host,
            )
            response = None
        if response is not None:
            return response
        if default is not None:
            return default
        raise KasaException(
            f"{request} not found in {responses} for device {self.host}"
        )

    async def _negotiate(self):
        """Perform initialization.

        We fetch the device info and the available components as early as possible.
        If the device reports supporting child devices, they are also initialized.
        """
        initial_query = {
            "component_nego": None,
            "get_device_info": None,
            "get_connect_cloud_state": None,
        }
        resp = await self.protocol.query(initial_query)

        # Save the initial state to allow modules access the device info already
        # during the initialization, which is necessary as some information like the
        # supported color temperature range is contained within the response.
        self._last_update.update(resp)
        self._info = self._try_get_response(resp, "get_device_info")

        # Create our internal presentation of available components
        self._components_raw = resp["component_nego"]
        self._components = {
            comp["id"]: int(comp["ver_code"])
            for comp in self._components_raw["component_list"]
        }

        if "child_device" in self._components and not self.children:
            await self._initialize_children()

    async def update(self, update_children: bool = False):
        """Update the device."""
        if self.credentials is None and self.credentials_hash is None:
            raise AuthenticationError("Tapo plug requires authentication.")

        first_update = self._last_update_time is None
        now = time.time()
        self._last_update_time = now

        if first_update:
            await self._negotiate()
            await self._initialize_modules()

        resp = await self._modular_update(first_update, now)

        # Call child update which will only update module calls, info is updated
        # from get_child_device_list. update_children only affects hub devices, other
        # devices will always update children to prevent errors on module access.
        if update_children or self.device_type != DeviceType.Hub:
            for child in self._children.values():
                await child._update()
        if child_info := self._try_get_response(
            self._last_update, "get_child_device_list", {}
        ):
            for info in child_info["child_device_list"]:
                self._children[info["device_id"]]._update_internal_state(info)

        for child in self._children.values():
            errors = []
            for child_module_name, child_module in child._modules.items():
                if not self._handle_module_post_update_hook(child_module):
                    errors.append(child_module_name)
            for error in errors:
                child._modules.pop(error)

        # We can first initialize the features after the first update.
        # We make here an assumption that every device has at least a single feature.
        if not self._features:
            await self._initialize_features()

        _LOGGER.debug(
            "Update completed %s: %s",
            self.host,
            self._last_update if first_update else resp,
        )

    def _handle_module_post_update_hook(self, module: SmartModule) -> bool:
        try:
            module._post_update_hook()
            return True
        except Exception as ex:
            _LOGGER.warning(
                "Error processing %s for device %s, module will be unavailable: %s",
                module.name,
                self.host,
                ex,
            )
            return False

    async def _modular_update(
        self, first_update: bool, update_time: float
    ) -> dict[str, Any]:
        """Update the device with via the module queries."""
        req: dict[str, Any] = {}
        # Keep a track of actual module queries so we can track the time for
        # modules that do not need to be updated frequently
        module_queries: list[SmartModule] = []
        mq = {
            module: query
            for module in self._modules.values()
            if (query := module.query())
        }
        for module, query in mq.items():
            if first_update and module.__class__ in FIRST_UPDATE_MODULES:
                module._last_update_time = update_time
                continue
            if (
                not module.MINIMUM_UPDATE_INTERVAL_SECS
                or not module._last_update_time
                or (update_time - module._last_update_time)
                >= module.MINIMUM_UPDATE_INTERVAL_SECS
            ):
                module_queries.append(module)
                req.update(query)

        _LOGGER.debug(
            "Querying %s for modules: %s",
            self.host,
            ", ".join(mod.name for mod in module_queries),
        )

        try:
            resp = await self.protocol.query(req)
        except Exception as ex:
            resp = await self._handle_modular_update_error(
                ex, first_update, ", ".join(mod.name for mod in module_queries), req
            )

        info_resp = self._last_update if first_update else resp
        self._last_update.update(**resp)
        self._info = self._try_get_response(info_resp, "get_device_info")

        # Call handle update for modules that want to update internal data
        errors = []
        for module_name, module in self._modules.items():
            if not self._handle_module_post_update_hook(module):
                errors.append(module_name)
        for error in errors:
            self._modules.pop(error)

        # Set the last update time for modules that had queries made.
        for module in module_queries:
            module._last_update_time = update_time

        return resp

    async def _handle_modular_update_error(
        self,
        ex: Exception,
        first_update: bool,
        module_names: str,
        requests: dict[str, Any],
    ) -> dict[str, Any]:
        """Handle an error on calling module update.

        Will try to call all modules individually
        and any errors such as timeouts will be set as a SmartErrorCode.
        """
        msg_part = "on first update" if first_update else "after first update"

        _LOGGER.error(
            "Error querying %s for modules '%s' %s: %s",
            self.host,
            module_names,
            msg_part,
            ex,
        )
        responses = {}
        for meth, params in requests.items():
            try:
                resp = await self.protocol.query({meth: params})
                responses[meth] = resp[meth]
            except Exception as iex:
                _LOGGER.error(
                    "Error querying %s individually for module query '%s' %s: %s",
                    self.host,
                    meth,
                    msg_part,
                    iex,
                )
                responses[meth] = SmartErrorCode.INTERNAL_QUERY_ERROR
        return responses

    async def _initialize_modules(self):
        """Initialize modules based on component negotiation response."""
        from .smartmodule import SmartModule

        # Some wall switches (like ks240) are internally presented as having child
        # devices which report the child's components on the parent's sysinfo, even
        # when they need to be accessed through the children.
        # The logic below ensures that such devices add all but whitelisted, only on
        # the child device.
        # It also ensures that devices like power strips do not add modules such as
        # firmware to the child devices.
        skip_parent_only_modules = False
        child_modules_to_skip = {}
        if self._parent and self._parent.device_type != DeviceType.Hub:
            skip_parent_only_modules = True

        for mod in SmartModule.REGISTERED_MODULES.values():
            if (
                skip_parent_only_modules and mod in NON_HUB_PARENT_ONLY_MODULES
            ) or mod.__name__ in child_modules_to_skip:
                continue
            if (
                mod.REQUIRED_COMPONENT in self._components
                or self.sys_info.get(mod.REQUIRED_KEY_ON_PARENT) is not None
            ):
                _LOGGER.debug(
                    "Device %s, found required %s, adding %s to modules.",
                    self.host,
                    mod.REQUIRED_COMPONENT,
                    mod.__name__,
                )
                module = mod(self, mod.REQUIRED_COMPONENT)
                if await module._check_supported():
                    self._modules[module.name] = module

        if (
            Module.Brightness in self._modules
            or Module.Color in self._modules
            or Module.ColorTemperature in self._modules
        ):
            self._modules[Light.__name__] = Light(self, "light")

    async def _initialize_features(self):
        """Initialize device features."""
        self._add_feature(
            Feature(
                self,
                id="device_id",
                name="Device ID",
                attribute_getter="device_id",
                category=Feature.Category.Debug,
                type=Feature.Type.Sensor,
            )
        )
        if "device_on" in self._info:
            self._add_feature(
                Feature(
                    self,
                    id="state",
                    name="State",
                    attribute_getter="is_on",
                    attribute_setter="set_state",
                    type=Feature.Type.Switch,
                    category=Feature.Category.Primary,
                )
            )

        if "signal_level" in self._info:
            self._add_feature(
                Feature(
                    self,
                    id="signal_level",
                    name="Signal Level",
                    attribute_getter=lambda x: x._info["signal_level"],
                    icon="mdi:signal",
                    category=Feature.Category.Info,
                    type=Feature.Type.Sensor,
                )
            )

        if "rssi" in self._info:
            self._add_feature(
                Feature(
                    self,
                    id="rssi",
                    name="RSSI",
                    attribute_getter=lambda x: x._info["rssi"],
                    icon="mdi:signal",
                    unit="dBm",
                    category=Feature.Category.Debug,
                    type=Feature.Type.Sensor,
                )
            )

        if "ssid" in self._info:
            self._add_feature(
                Feature(
                    device=self,
                    id="ssid",
                    name="SSID",
                    attribute_getter="ssid",
                    icon="mdi:wifi",
                    category=Feature.Category.Debug,
                    type=Feature.Type.Sensor,
                )
            )

        if "overheated" in self._info:
            self._add_feature(
                Feature(
                    self,
                    id="overheated",
                    name="Overheated",
                    attribute_getter=lambda x: x._info["overheated"],
                    icon="mdi:heat-wave",
                    type=Feature.Type.BinarySensor,
                    category=Feature.Category.Info,
                )
            )

        # We check for the key available, and not for the property truthiness,
        # as the value is falsy when the device is off.
        if "on_time" in self._info:
            self._add_feature(
                Feature(
                    device=self,
                    id="on_since",
                    name="On since",
                    attribute_getter="on_since",
                    icon="mdi:clock",
                    category=Feature.Category.Debug,
                    type=Feature.Type.Sensor,
                )
            )

        for module in self.modules.values():
            module._initialize_features()
            for feat in module._module_features.values():
                self._add_feature(feat)
        for child in self._children.values():
            await child._initialize_features()

    @property
    def is_cloud_connected(self) -> bool:
        """Returns if the device is connected to the cloud."""
        if Module.Cloud not in self.modules:
            return False
        return self.modules[Module.Cloud].is_connected

    @property
    def sys_info(self) -> dict[str, Any]:
        """Returns the device info."""
        return self._info  # type: ignore

    @property
    def model(self) -> str:
        """Returns the device model."""
        return str(self._info.get("model"))

    @property
    def alias(self) -> str | None:
        """Returns the device alias or nickname."""
        if self._info and (nickname := self._info.get("nickname")):
            return base64.b64decode(nickname).decode()
        else:
            return None

    @property
    def time(self) -> datetime:
        """Return the time."""
        if (self._parent and (time_mod := self._parent.modules.get(Module.Time))) or (
            time_mod := self.modules.get(Module.Time)
        ):
            return time_mod.time

        # We have no device time, use current local time.
        return datetime.now(timezone.utc).astimezone().replace(microsecond=0)

    @property
    def on_since(self) -> datetime | None:
        """Return the time that the device was turned on or None if turned off."""
        if (
            not self._info.get("device_on")
            or (on_time := self._info.get("on_time")) is None
        ):
            return None

        on_time = cast(float, on_time)
        return self.time - timedelta(seconds=on_time)

    @property
    def timezone(self) -> dict:
        """Return the timezone and time_difference."""
        ti = self.time
        return {"timezone": ti.tzname()}

    @property
    def hw_info(self) -> dict:
        """Return hardware info for the device."""
        return {
            "sw_ver": self._info.get("fw_ver"),
            "hw_ver": self._info.get("hw_ver"),
            "mac": self._info.get("mac"),
            "type": self._info.get("type"),
            "hwId": self._info.get("device_id"),
            "dev_name": self.alias,
            "oemId": self._info.get("oem_id"),
        }

    @property
    def location(self) -> dict:
        """Return the device location."""
        loc = {
            "latitude": cast(float, self._info.get("latitude", 0)) / 10_000,
            "longitude": cast(float, self._info.get("longitude", 0)) / 10_000,
        }
        return loc

    @property
    def rssi(self) -> int | None:
        """Return the rssi."""
        rssi = self._info.get("rssi")
        return int(rssi) if rssi else None

    @property
    def mac(self) -> str:
        """Return the mac formatted with colons."""
        return str(self._info.get("mac")).replace("-", ":")

    @property
    def device_id(self) -> str:
        """Return the device id."""
        return str(self._info.get("device_id"))

    @property
    def internal_state(self) -> Any:
        """Return all the internal state data."""
        return self._last_update

    def _update_internal_state(self, info):
        """Update the internal info state.

        This is used by the parent to push updates to its children.
        """
        self._info = info

    async def _query_helper(
        self, method: str, params: dict | None = None, child_ids=None
    ) -> Any:
        res = await self.protocol.query({method: params})

        return res

    @property
    def ssid(self) -> str:
        """Return ssid of the connected wifi ap."""
        ssid = self._info.get("ssid")
        ssid = base64.b64decode(ssid).decode() if ssid else "No SSID"
        return ssid

    @property
    def has_emeter(self) -> bool:
        """Return if the device has emeter."""
        return Module.Energy in self.modules

    @property
    def is_on(self) -> bool:
        """Return true if the device is on."""
        return bool(self._info.get("device_on"))

    async def set_state(self, on: bool):  # TODO: better name wanted.
        """Set the device state.

        See :meth:`is_on`.
        """
        return await self.protocol.query({"set_device_info": {"device_on": on}})

    async def turn_on(self, **kwargs):
        """Turn on the device."""
        await self.set_state(True)

    async def turn_off(self, **kwargs):
        """Turn off the device."""
        await self.set_state(False)

    def update_from_discover_info(self, info):
        """Update state from info from the discover call."""
        self._discovery_info = info
        self._info = info

    async def wifi_scan(self) -> list[WifiNetwork]:
        """Scan for available wifi networks."""

        def _net_for_scan_info(res):
            return WifiNetwork(
                ssid=base64.b64decode(res["ssid"]).decode(),
                cipher_type=res["cipher_type"],
                key_type=res["key_type"],
                channel=res["channel"],
                signal_level=res["signal_level"],
                bssid=res["bssid"],
            )

        _LOGGER.debug("Querying networks")

        resp = await self.protocol.query({"get_wireless_scan_info": {"start_index": 0}})
        networks = [
            _net_for_scan_info(net) for net in resp["get_wireless_scan_info"]["ap_list"]
        ]
        return networks

    async def wifi_join(self, ssid: str, password: str, keytype: str = "wpa2_psk"):
        """Join the given wifi network.

        This method returns nothing as the device tries to activate the new
        settings immediately instead of responding to the request.

        If joining the network fails, the device will return to the previous state
        after some delay.
        """
        if not self.credentials:
            raise AuthenticationError("Device requires authentication.")

        payload = {
            "account": {
                "username": base64.b64encode(
                    self.credentials.username.encode()
                ).decode(),
                "password": base64.b64encode(
                    self.credentials.password.encode()
                ).decode(),
            },
            "wireless": {
                "key_type": keytype,
                "password": base64.b64encode(password.encode()).decode(),
                "ssid": base64.b64encode(ssid.encode()).decode(),
            },
            "time": self.internal_state["get_device_time"],
        }

        # The device does not respond to the request but changes the settings
        # immediately which causes us to timeout.
        # Thus, We limit retries and suppress the raised exception as useless.
        try:
            return await self.protocol.query({"set_qs_info": payload}, retry_count=0)
        except DeviceError:
            raise  # Re-raise on device-reported errors
        except KasaException:
            _LOGGER.debug("Received an expected for wifi join, but this is expected")

    async def update_credentials(self, username: str, password: str):
        """Update device credentials.

        This will replace the existing authentication credentials on the device.
        """
        time_data = self.internal_state["get_device_time"]
        payload = {
            "account": {
                "username": base64.b64encode(username.encode()).decode(),
                "password": base64.b64encode(password.encode()).decode(),
            },
            "time": time_data,
        }
        return await self.protocol.query({"set_qs_info": payload})

    async def set_alias(self, alias: str):
        """Set the device name (alias)."""
        return await self.protocol.query(
            {"set_device_info": {"nickname": base64.b64encode(alias.encode()).decode()}}
        )

    async def reboot(self, delay: int = 1) -> None:
        """Reboot the device.

        Note that giving a delay of zero causes this to block,
        as the device reboots immediately without responding to the call.
        """
        await self.protocol.query({"device_reboot": {"delay": delay}})

    async def factory_reset(self) -> None:
        """Reset device back to factory settings.

        Note, this does not downgrade the firmware.
        """
        await self.protocol.query("device_reset")

    @property
    def device_type(self) -> DeviceType:
        """Return the device type."""
        if self._device_type is not DeviceType.Unknown:
            return self._device_type

        self._device_type = self._get_device_type_from_components(
            list(self._components.keys()), self._info["type"]
        )

        return self._device_type

    @staticmethod
    def _get_device_type_from_components(
        components: list[str], device_type: str
    ) -> DeviceType:
        """Find type to be displayed as a supported device category."""
        if "HUB" in device_type:
            return DeviceType.Hub
        if "PLUG" in device_type:
            if "child_device" in components:
                return DeviceType.Strip
            return DeviceType.Plug
        if "light_strip" in components:
            return DeviceType.LightStrip
        if "SWITCH" in device_type and "child_device" in components:
            return DeviceType.WallSwitch
        if "dimmer_calibration" in components:
            return DeviceType.Dimmer
        if "brightness" in components:
            return DeviceType.Bulb
        if "SWITCH" in device_type:
            return DeviceType.WallSwitch
        if "SENSOR" in device_type:
            return DeviceType.Sensor
        if "ENERGY" in device_type:
            return DeviceType.Thermostat
        _LOGGER.warning("Unknown device type, falling back to plug")
        return DeviceType.Plug
