#
# YamahaControl App
# Control a Yamaha RX-V1500 receiver connected via RS232 using MQTT
#

import logging
import logging.config
import time
import re
import subprocess
import socket
import json
from signal import signal, SIGINT
from types import SimpleNamespace

# Install packages for RS232 and MQTT connection, if required
try:
    import asyncio
    import yaml
    import serial_asyncio
    from paho.mqtt import client as mqtt_client
except:
    print("Installing python package dependencies...")
    pip_install = subprocess.Popen(
        ["pip3", "install", "-r", "requirements.txt"])
    pip_install.communicate()
    import asyncio
    import yaml
    import serial_asyncio
    from paho.mqtt import client as mqtt_client


STX = b'\x02'  # Start of Text
ETX = b'\x03'  # End of Text
DC1 = b'\x11'  # Device Control 1
DC2 = b'\x12'  # Device Control 2
DC3 = b'\x13'  # Device Control 3
DC4 = b'\x14'  # Device Control 4
DEL = b'\x7F'  # Delete


class Config:
    """Class for parsing `config.yaml`."""

    def __init__(self, file='config.yaml'):
        """Initialize Config class and read config file."""
        logging.info("Reading %s", file)
        try:
            with open(file, 'r') as filehandle:
                config = yaml.load(filehandle, Loader=yaml.SafeLoader)
                if "logging" in config:
                    config["logging"].setdefault('version', 1)
                    logging.config.dictConfig(config["logging"])
                self._parse_mqtt(config)
                self._parse_serial(config)
                self._parse_limits(config)

        except FileNotFoundError as ex:
            logging.error("Configuration file %s not found: %s", file, ex)
            exit(ex.errno)

    def _parse_mqtt(self, config):
        if not "mqtt" in config:
            logging.error(
                "MQTT configuration not found in configuration file.")
            exit(1)
        self.mqtt = SimpleNamespace(**config["mqtt"])

        if not hasattr(self.mqtt, "host"):
            raise ValueError("MQTT broker not defined!")

        self.mqtt.__dict__.setdefault("client_id", "rxv1500-mqtt")
        self.mqtt.__dict__.setdefault("topic_prefix", "")
        self.mqtt.__dict__.setdefault("port", 1883)
        self.mqtt.__dict__.setdefault("username", "")
        self.mqtt.__dict__.setdefault("password", "")
        self.mqtt.__dict__.setdefault("qos", 0)
        self.mqtt.__dict__.setdefault("retain", False)
        self.mqtt.__dict__.setdefault("keepalive", 60)

        self.mqtt.discovery = SimpleNamespace(
            **self.mqtt.discovery if hasattr(self.mqtt, "discovery") else {})
        self.mqtt.discovery.__dict__.setdefault(
            "unique_id", "yamaha-rxv1500-1")
        self.mqtt.discovery.__dict__.setdefault("via_device_id", "")
        self.mqtt.discovery.__dict__.setdefault(
            "topic_prefix", "homeassistant")

    def _parse_serial(self, config):
        self.serial = SimpleNamespace(
            **config["serial"] if "serial" in config else {})
        self.serial.__dict__.setdefault("device", "/dev/ttyUSB0")

    def _parse_limits(self, config):
        self.limits = SimpleNamespace(
            **config["limits"] if "limits" in config else {})
        self.limits.__dict__.setdefault("volume", -20)  # or None


class YamahaControl:
    """
    This is the main class of the Controller. __init__() will initialize all components.
    """

    class RemoteEventDiscovery:
        def __init__(self, controller, config, subscription, discovery_callback, state_only=False) -> None:
            self.controller = controller
            self.config = config
            self.subscription = subscription
            self.discovery_callback = discovery_callback
            self.state_only = state_only

            # functions alias
            self.publish = self.controller.mqtt.publish

        async def announce_periodically(self, period=5*60):
            while True:
                await self.announce()
                await asyncio.sleep(period)

        async def announce(self):
            # Send discovery message
            device_class, payload = self.discovery_callback()
            if not payload:
                return

            payload.update({
                "~": self.subscription.topic_state,
                "stat_t": "~",
            })
            if not self.state_only:
                payload.update({
                    "cmd_t": "~/set",
                })

            if device_class == "switch":
                payload.update({
                    "pl_on": "on",
                    "pl_off": "off"
                })

            # Create and publish message
            topic = "%s/%s/%s/%s/config" % (
                self.config.topic_prefix,
                device_class,
                self.config.unique_id,
                self.subscription.topic_extension
            )
            await self.publish(topic, json.dumps(payload), retain=True)

        async def revert(self):
            raise NotImplementedError

    class RemoteEventSubscription:
        def __init__(self, controller, config, topic_extension, on_event_callback, state_only=False):
            self.controller = controller
            self.config = config
            self.topic_extension = topic_extension
            self.topic_state = str(
                "%s/%s/%s" % (self.config.topic_prefix, self.config.client_id, topic_extension))
            self.on_event_callback = on_event_callback
            self.state_only = state_only

            # functions alias
            self.publish_state = self.controller.mqtt.publish_state

        async def subscribe(self):
            self.controller.log.info(
                "Publishing states for topic: %s" % str(self.topic_state))

            # Subscribe for command topic
            if not self.state_only:
                self.topic_command = str("%s/%s" % (self.topic_state, "set"))
                self.controller.mqtt.client.subscribe(
                    self.topic_command, qos=1)
                self.controller.mqtt.client.message_callback_add(
                    self.topic_command, self.on_event_callback)
                self.controller.log.info(
                    "Registered listener for topic: %s" % str(self.topic_command))
            else:
                self.topic_command = None

        async def unsubscribe(self):
            if self.controller.mqtt and self.topic_command:
                self.controller.mqtt.client.message_callback_remove(
                    self.topic_command)
                self.controller.mqtt.client.unsubscribe(self.topic_command)
                self.controller.log.info(
                    "Removed listener for topic: %s" % str(self.topic_command))

        def __str__(self):
            return str("RemoteEventSubscription: %s" % self.topic_state)

    class EntityBase:
        """
        Base class for each configuration entity
        """
        options = {
            # Yamaha-ID: (HASS-State, Set-Cmd)
        }

        def __init__(self, controller, topic_extension, state_only=False):
            if not hasattr(self, "name") or not self.name:
                self.name = topic_extension
            self.controller = controller

            self.subscription = YamahaControl.RemoteEventSubscription(
                self.controller, self.controller.config.mqtt, topic_extension, self.on_mqtt_cmd_for_rc, state_only)
            self.controller.remote_subscriptions.add(self.subscription)

            # Init discovery messages and start periodic sending
            self.discovery = YamahaControl.RemoteEventDiscovery(
                self.controller, self.controller.config.mqtt.discovery, self.subscription, self.mqtt_discovery_config, state_only)
            self.controller.discovery_tasks.add(
                asyncio.Task(self.discovery.announce_periodically()))

        def __str__(self):
            return str("Entity: %s" % self.name)

        async def on_rc_state_update(self, state):
            self.controller.log.info("[RcState] State: " + str(state))
            if state in self.options and len(self.options[state]) > 0:
                self.controller.log.info("[RcState] Going to publish state >>%s<< for topic >>%s<<" % (
                    str(self.options[state][0]), self.subscription.topic_state))

                # Publish new receiver state with mqtt
                await self.subscription.publish_state(
                    self.subscription.topic_state, self.options[state][0], retain=True)
            else:
                raise NotImplementedError

        async def write_rc(self, state):
            for opt in self.options.values():
                if opt[0] == state:
                    if opt[1] is not None:
                        self.controller.log.info('[WriteRc] Setting >>' + str(self) + '<< (' + str(
                            self.subscription.topic_state) + ') to >>' + str(state) + '<<.')
                        await self.controller.rs232.write(opt[1])
                    return
            raise NotImplementedError

        def on_mqtt_cmd_for_rc(self, mqtt_client, userdata, message):
            #self.controller.log.debug("[MqttMessage] EVENT: " + str(message.topic) + " with " + str(userdata))
            state_new = message.payload.decode("utf-8")

            self.controller.log.info("[MqttMessage] Processing command >>" + str(
                message.topic) + "<< with data >>" + str(state_new) + "<<")
            self.controller.loop.create_task(self.write_rc(state_new))

        def mqtt_discovery_config(self, device_class=None, icon=None):
            if device_class is None:
                return None, None

            payload = {
                "unique_id": "%s_%s" % (self.controller.config.mqtt.discovery.unique_id, self.name),
                "object_id": "%s_%s" % (self.controller.config.mqtt.discovery.unique_id, self.name),
                "name": str(self),
                "device": {
                    "identifiers": str(self.controller.config.mqtt.discovery.unique_id),
                    "manufacturer": "Yamaha",
                    "model": "RX-V1500",
                    "name": "Yamaha RX-V1500 A/V Receiver"
                }
            }
            if icon:
                payload["icon"] = icon
            return device_class, payload

    class GenericSensorEntity(EntityBase):
        def __init__(self, controller, topic_extension, entity_name, icon=None):
            self.name = entity_name
            self.icon = icon
            super().__init__(controller, topic_extension, state_only=True)

        def __str__(self):
            return self.name

        async def on_rc_state_update(self, state):
            await self.subscription.publish_state(
                self.subscription.topic_state, state, retain=True)

        async def write_rc(self, state):
            self.controller.log.info(
                "ERROR! Write on GenericSensor is not allowed! Tried: %s" % (state))

        def mqtt_discovery_config(self, device_class=None, icon=None):
            i = icon if icon else self.icon
            dc = device_class if device_class else "sensor"
            return super().mqtt_discovery_config(dc, icon=i)

    class GenericBinarySensorEntity(GenericSensorEntity):
        def mqtt_discovery_config(self):
            dev_cl, pl = super().mqtt_discovery_config("binary_sensor", icon=self.icon)
            pl.update({
                "payload_on": "On",
                "payload_off": "Off"
            })
            return dev_cl, pl

    class PowerEntity(EntityBase):
        options = {
            # Yamaha-ID: (HASS-State, Set-Cmd)
            'On': ('on', '07a1d'),  # Power On
            'Off': ('off', '07a1e')  # Power Off
        }

        async def write_rc(self, state):
            if state == 'on':
                self.controller.log.info('[WriteRc] Setting ' + str(self) + ' (' + str(
                    self.subscription.topic_state) + ') to "' + str(state) + '".')
                commands = [
                    DC1.decode("utf-8") + "000",
                    "20000",
                    "20100",
                    self.options['On'][1]
                ]
                await self.controller.rs232.write(commands)
            elif state == 'off':
                self.controller.log.info('[WriteRc] Setting ' + str(self) + ' (' + str(
                    self.subscription.topic_state) + ') to "' + str(state) + '".')
                await self.controller.rs232.write(self.options['Off'][1])
            else:
                raise NotImplementedError

        def mqtt_discovery_config(self):
            return super().mqtt_discovery_config("switch", icon="mdi:power")

        def __str__(self):
            return str("Power")

    class ResetEntity(EntityBase):
        options = {
            # Yamaha-ID: (HASS-State, Set-Cmd)
            # Reset all RS232 controlled settings # (DC3, DEL, DEL, DEL, ETX)
            'On': ('on', "%s%s%s%s" % (DC3.decode("utf-8"), DEL.decode("utf-8"), DEL.decode("utf-8"), DEL.decode("utf-8"))),
            'Off': ('off', None)
        }

        def mqtt_discovery_config(self):
            return super().mqtt_discovery_config("switch")

        def __str__(self):
            return str("Reset")

    class PowerZone1Entity(EntityBase):
        options = {
            # Yamaha-ID: (HASS-State, Set-Cmd)
            'On': ('on', '07e7e'),  # Power On
            'Off': ('off', '07e7f')  # Power Off
        }

        def mqtt_discovery_config(self):
            return super().mqtt_discovery_config("switch", icon="mdi:power")

        def __str__(self):
            return str("Power Zone 1")

    class PowerZone2Entity(EntityBase):
        options = {
            # Yamaha-ID: (HASS-State, Set-Cmd)
            'On': ('on', '07eba'),  # Power On
            'Off': ('off', '07ebb')  # Power Off
        }

        def mqtt_discovery_config(self):
            return super().mqtt_discovery_config("switch", icon="mdi:power")

        def __str__(self):
            return str("Power Zone 2")

    class SpeakersAEntity(EntityBase):
        options = {
            # Yamaha-ID: (HASS-State, Set-Cmd)
            'On': ('on', '07eab'),  # Power On
            'Off': ('off', '07eac')  # Power Off
        }

        def mqtt_discovery_config(self):
            return super().mqtt_discovery_config("switch", icon="mdi:speaker")

        def __str__(self):
            return str("Speakers A")

    class SpeakersBEntity(EntityBase):
        options = {
            # Yamaha-ID: (HASS-State, Set-Cmd)
            'On': ('on', '07ead'),  # Power On
            'Off': ('off', '07eae')  # Power Off
        }

        def mqtt_discovery_config(self):
            return super().mqtt_discovery_config("switch", icon="mdi:speaker")

        def __str__(self):
            return str("Speakers B")

    class InputSourceEntity(EntityBase):
        options = {
            # Yamaha-ID: (HASS-State, Set-Cmd)
            'Phono': ('phono', '07a14'),
            'CD': ('cd', '07a15'),
            'Tuner': ('tuner', '07a16'),
            'CDR': ('cdr', '07a19'),
            'MDTape': ('md-tape', '07ac9'),
            'DVD': ('dvd', '07ac1'),
            'DTVLD': ('dtv', '07a54'),
            'Cable': ('cbl-sat', '07ac0'),
            'VCR1': ('vcr1', '07a0f'),
            'VCR2': ('dvr-vcr2', '07a13'),
            'VAux': ('vaux', '07a55')
        }

        def mqtt_discovery_config(self):
            dev_cl, pl = super().mqtt_discovery_config("select", icon="mdi:import")
            pl["options"] = [x[0] for x in self.options.values()]
            return dev_cl, pl

        def __str__(self):
            return str("Input Source")

    class InputModeEntity(EntityBase):
        options = {
            # Yamaha-ID: (HASS-State, Set-Cmd)
            'Auto': ('auto', '07ea6'),
            'DTS': ('dts', '07ea8'),
            'Analog': ('analog', '07eaa'),
            # 'AnalogOnly': ('analog', '')
        }

        def mqtt_discovery_config(self):
            dev_cl, pl = super().mqtt_discovery_config(
                "select", icon="mdi:surround-sound-5-1")
            pl["options"] = [x[0] for x in self.options.values()]
            return dev_cl, pl

        def __str__(self):
            return str("Input Mode")

    class TunerPresetEntity(EntityBase):
        options = {
            # Yamaha-ID: (HASS-State, Set-Cmd)
            '1': ('1', '07ae5'),
            '2': ('2', '07ae6'),
            '3': ('3', '07ae7'),
            '4': ('4', '07ae8'),
            '5': ('5', '07ae9'),
            '6': ('6', '07aea'),
            '7': ('7', '07aeb'),
            '8': ('8', '07aec'),
        }

        def mqtt_discovery_config(self):
            dev_cl, pl = super().mqtt_discovery_config("select", icon="mdi:tune")
            pl["options"] = [x[0] for x in self.options.values()]
            return dev_cl, pl

        def __str__(self):
            return str("Tuner Preset")

        async def write_rc(self, state):
            # Format in HA: '2.0' (string) -> only use '2'
            if len(state) > 0:
                await super().write_rc(state[0])

    class OsdEntity(EntityBase):
        options = {
            # Yamaha-ID: (HASS-State, Set-Cmd)
            'Off': ('off', '07eb0'),
            'Short': ('short', '07eb1'),
            'Full': ('full', '07eb2'),
        }

        def mqtt_discovery_config(self):
            dev_cl, pl = super().mqtt_discovery_config("select", icon="mdi:fullscreen")
            pl["options"] = [x[0] for x in self.options.values()]
            return dev_cl, pl

        def __str__(self):
            return str("OSD")

    class DspEntity(EntityBase):
        options = {
            # Yamaha-ID: (HASS-State, Set-Cmd)
            'straight': ('straight', '07ee0'),
            'concert hall': ('concert hall', '07ee5'),
            'jazz club': ('jazz club', '07eec'),
            'rock concert': ('rock concert', '07eed'),
            'disco': ('disco', '07ef0'),
            'game': ('game', '07ef2'),
            'music video': ('music video', '07ef3'),
            'mono movie': ('mono movie', '07ef7'),
            'variety sports': ('variety sports', '07ef8'),
            'spectacle': ('spectacle', '07ef9'),
            'sci-fi': ('sci-fi', '07efa'),
            'adventure': ('adventure', '07efb'),
            'general': ('general', '07efc'),
            'thx cinema': ('thx cinema', '07ec2'),
            'pro logic sur standard': ('pro logic sur standard', '07efd'),
            'pro logic sur enhanced': ('pro logic sur enhanced', '07efe'),
            'pro logic II movie': ('pro logic II movie', ''),
            'pro logic II music': ('pro logic II music', ''),
            'pro logic II game': ('pro logic II game', '07ec7'),
            'dts neo 6 cinema': ('dts neo 6 cinema', ''),
            'dts neo 6 music': ('dts neo 6 music', ''),
            '2ch stereo': ('2ch stereo', ''),
            'direct stereo': ('direct stereo', '07ec1'),
            '7ch stereo': ('7ch stereo', '07eff')
        }

        def mqtt_discovery_config(self):
            dev_cl, pl = super().mqtt_discovery_config("select", icon="mdi:map-marker")
            pl["options"] = [x[0] for x in self.options.values()]
            return dev_cl, pl

        def __str__(self):
            return str("DSP")

    class MasterVolumeEntity(EntityBase):
        VOL_MIN = -80.0
        VOL_STEP = 0.5

        def __str__(self):
            return str("Master Volume")

        def limit(self, volume, log=True):
            if not isinstance(volume, float):
                volume = float(volume)
            if self.controller.config.limits.volume is not None and volume > self.controller.config.limits.volume:
                volume = float(self.controller.config.limits.volume)
                if log:
                    self.controller.log.info(
                        "[V] Volume limit reached. Changed value to: %d" % (volume))
            return volume

        def percentage(self, volume):
            # scale from [-80,0] to [0,1]
            return float((1 - 0) * (volume - (self.VOL_MIN)) / (0 - (self.VOL_MIN)) + 0)

        async def on_rc_state_update(self, vol_hex):
            # Process volume from hex string to -dB
            new_db_val = (YamahaControl.hex_to_dec(
                vol_hex) * self.VOL_STEP) - 99.5

            # Update state
            await self.subscription.publish_state(
                self.subscription.topic_state, new_db_val, retain=True)
            await self.subscription.publish_state(
                self.subscription.topic_state + "/details",
                json.dumps({
                    "volume_percentage": self.percentage(new_db_val)
                }),
                retain=True
            )

        async def write_rc(self, vol_new):
            # Process volume from -dB string to hex
            new_normed = int((self.limit(vol_new) + 99.5) / self.VOL_STEP)
            raw_db_val = str(
                str(YamahaControl.dec_to_hex(new_normed))[2:]).upper()
            self.controller.log.debug(
                "[V] Volume to set: %s" % str(raw_db_val))
            await self.controller.rs232.write("230" + str(raw_db_val))

        def mqtt_discovery_config(self):
            dev_cl, pl = super().mqtt_discovery_config("number", icon="mdi:volume-high")
            pl.update({
                "min": self.VOL_MIN,
                "max": float(self.controller.config.limits.volume),
                "step": self.VOL_STEP,
                "json_attributes_topic": "~/details"
            })
            return dev_cl, pl

    class MuteEntity(EntityBase):
        options = {
            # Yamaha-ID: (HASS-State, Set-Cmd)
            'On': ('on', '07ea2'),  # Mute
            'Off': ('off', '07ea3')  # Unmute
        }

        def __str__(self):
            return str("Mute")

        def mqtt_discovery_config(self):
            return super().mqtt_discovery_config("switch", icon="mdi:volume-mute")

    def __init__(self, config):
        """
        ENTRYPOINT / INIT of YamahaControl / Controller
        """
        self.config = config

        # Setup logging
        self.log = logging.getLogger("controller")
        self.log.info("Starting YamahaControl...")

        # Create asyncio event loop
        self.loop = asyncio.get_event_loop()

        # Setup physical device access via serial connection
        self.rs232 = SerialClient(
            self.loop, self.config.serial, self.process_serial_data)
        self.loop.create_task(self.rs232.run())

        # Setup MQTT client
        # Callback: Generate rc event list which registers MQTT subscriptions
        self.remote_subscriptions = set()
        self.discovery_tasks = set()
        self.rc_event_list = None
        self.mqtt = MqttClient(
            self.loop, self.config.mqtt, self._setup_rc_event_list, self.clear)
        self.loop.create_task(self.mqtt.run())

    def __del__(self):
        """
        Destructor of Controller
        """
        self.terminate()

    def terminate(self):
        if self.loop and self.loop.is_running():
            logging.debug("Terminating event loop...")
            self.loop.stop()
    
    async def clear(self):
        await self._clear_discovery_tasks()
        await self._clear_remote_subscriptions()

    async def async_terminate(self):
        await self.clear()

        if self.mqtt:
            self.log.info("Terminating MQTT connection...")
            self.mqtt.terminate()
        self.mqtt = None

        if self.rs232:
            self.log.info("Terminating RS232 connection...")
            self.rs232.terminate()
        self.rs232 = None

    def run(self):
        logging.debug("Starting event loop...")
        self.loop.run_forever()
        self.loop.run_until_complete(self.async_terminate())
        self.loop.close()
        logging.debug("Closed event loop.")

    async def _setup_rc_event_list(self):
        # Define possible events/messages from RX-V1500 and initialize Entity Objects
        self.rc_event_list = {
            '00': {
                '_content': 'NoGuard',
                '00': 'Ok',
                '01': 'Busy',
                '02': 'PowerOff'
            },
            '01': {
                '_content': 'Warning',
                '00': 'OverCurrent',
                '01': 'DCDetect',
                '02': 'PowerTrouble',
                '03': 'OverHeat'
            },
            '10': {
                '_content': 'Format',
                '_object': YamahaControl.GenericSensorEntity(self, 'playback-format', 'Playback Format'),
                '00': 'ExternalDecoder',
                '01': 'Analog',
                '02': 'PCM',
                '03': 'DD',
                '04': 'DD20',
                '05': 'DDKaraoke',
                '06': 'DD61',
                '07': 'DTS',
                '08': 'DTSES',
                '09': 'Digital'
            },
            '11': {
                '_content': 'Fs',
                '_object': YamahaControl.GenericSensorEntity(self, 'playback-bitrate', 'Playback Bitrate'),
                '00': 'Analog',
                '01': '32kHz',
                '02': '441kHz',
                '03': '48kHz',
                '04': '64kHz',
                '05': '882kHz',
                '06': '96kHz',
                '07': 'Unknown'
            },
            '12': {
                '_content': '61ES',
                '00': 'On',
                '01': 'Off'
            },
            '13': {
                '_content': 'ThrBypass',
                '00': 'Normal',
                '01': 'Bypass'
            },
            '14': {
                '_content': 'REDDTS',
                '00': 'Release',
                '01': 'Wait'
            },
            '15': {
                '_content': 'Tuner',
                '00': 'NotTuned',
                '01': 'Tuned'
            },
            '20': {
                '_content': 'Power',
                '_object': YamahaControl.PowerEntity(self, 'power'),
                '00': 'Off',
                '01': 'On'
            },
            '21': {
                '_content': 'Input',
                '_object': YamahaControl.InputSourceEntity(self, 'input-source'),
                # 0x is for 6ch off, 1x is for 6ch on
                '00': 'Phono',
                '01': 'CD',
                '02': 'Tuner',
                '03': 'CDR',
                '04': 'MDTape',
                '05': 'DVD',
                '06': 'DTVLD',
                '07': 'Cable',
                '09': 'VCR1',
                '0A': 'VCR2',
                '0B': 'VAux',
                '10': 'Phono',
                '11': 'CD',
                '12': 'Tuner',
                '13': 'CDR',
                '14': 'MDTape',
                '15': 'DVD',
                '16': 'DTVLD',
                '17': 'Cable',
                '19': 'VCR1',
                '1A': 'VCR2',
                '1B': 'VAux'
            },
            '22': {
                '_content': 'InputMode',
                '_object': YamahaControl.InputModeEntity(self, 'input-mode'),
                '00': 'Auto',
                '02': 'DTS',
                '04': 'Analog',
                '05': 'AnalogOnly'
            },
            '23': {
                '_content': 'Mute',
                '_object': YamahaControl.MuteEntity(self, 'mute'),
                '00': 'Off',
                '01': 'On'
            },
            '24': {
                '_content': 'Zone2Input',
                '00': 'Phono',
                '01': 'CD',
                '02': 'Tuner',
                '03': 'CDR',
                '04': 'MDTape',
                '05': 'DVD',
                '06': 'DTVLD',
                '07': 'Cable',
                '09': 'VCR1',
                '0A': 'VCR2',
                '0B': 'VAux'
            },
            '25': {
                '_content': 'Zone2Mute',
                '_object': YamahaControl.PowerZone2Entity(self, 'power-zone-2'),
                '00': 'Off',
                '01': 'On'
            },
            '26': {
                '_content': 'MasterVolume',
                '_object': YamahaControl.MasterVolumeEntity(self, 'output-volume')
            },
            '28': {
                '_content': 'DSP',
                '_object': YamahaControl.DspEntity(self, 'dsp'),
                '85': 'straight',
                '05': 'concert hall',
                '0E': 'jazz club',
                '10': 'rock concert',
                '14': 'disco',
                '16': 'game',
                '18': 'music video',
                '20': 'mono movie',
                '21': 'variety sports',
                '24': 'spectacle',
                '25': 'sci-fi',
                '28': 'adventure',
                '29': 'general',
                '36': 'thx cinema',
                '2C': 'pro logic sur standard',
                '2D': 'pro logic sur enhanced',
                '30': 'pro logic II movie',
                '31': 'pro logic II music',
                '38': 'pro logic II game',
                '32': 'dts neo 6 cinema',
                '33': 'dts neo 6 music',
                '34': '2ch stereo',
                '35': 'direct stereo',
                '17': '7ch stereo',
            },
            '29': {
                '_content': 'TunerPage',
                '00': 'A',
                '01': 'B',
                '02': 'C',
                '03': 'D',
                '04': 'E'
            },
            '2A': {
                '_content': 'TunerPreset',
                '_object': YamahaControl.TunerPresetEntity(self, 'input-tuner-preset'),
                '00': '1',
                '01': '2',
                '02': '3',
                '03': '4',
                '04': '5',
                '05': '6',
                '06': '7',
                '07': '8'
            },
            '2B': {
                '_content': 'OSD',
                '_object': YamahaControl.OsdEntity(self, 'input-osd'),
                '00': 'Full',
                '01': 'Short',
                '02': 'Off'
            },
            '2C': {
                '_content': 'Sleep',
                '00': '120',
                '01': '90',
                '02': '60',
                '03': '30',
                '04': 'Off'
            },
            '2D': {
                '_content': '61ESKey',
                '00': 'Off',
                '01': 'On'
            },
            '2E': {
                '_content': 'SpkrRelayA',
                '_object': YamahaControl.SpeakersAEntity(self, 'speakers-a'),
                '00': 'Off',
                '01': 'On'
            },
            '2F': {
                '_content': 'SpkrRelayB',
                '_object': YamahaControl.SpeakersBEntity(self, 'speakers-b'),
                '00': 'Off',
                '01': 'On'
            },
            '30': {
                '_content': 'HomeBank',
                '00': 'Main',
                '01': 'A',
                '02': 'B',
                '03': 'C'
            },
            '31': {
                '_content': 'HomePreset',
                '00': 'A',
                '01': 'B',
                '02': 'C'
            },
            '32': {
                '_content': 'VolumeBank',
                '00': 'Main',
                '01': 'A',
                '02': 'B',
                '03': 'C'
            },
            '33': {
                '_content': 'VolumePreset',
                '00': 'A',
                '01': 'B',
                '02': 'C'
            },
            '34': {
                '_content': 'Headphone',
                '_object': YamahaControl.GenericBinarySensorEntity(self, 'headphone', 'Headphone', icon='mdi:headphone'),
                '00': 'Off',
                '01': 'On'
            },
            '35': {
                '_content': 'FMAM',
                '00': 'FM',
                '01': 'AM'
            },
            '99': {   # ToDo: Unknown
                '_content': 'Z1',
                '_object': YamahaControl.PowerZone1Entity(self, "power-zone-1")
            }
        }

        # Finally, create subscriptions at remote broker
        await self._create_remote_subscriptions()

    async def _create_remote_subscriptions(self):
        for sub in self.remote_subscriptions:
            await sub.subscribe()

    async def _clear_remote_subscriptions(self):
        for sub in self.remote_subscriptions:
            await sub.unsubscribe()
        self.remote_subscriptions = set()

    async def _clear_discovery_tasks(self):
        for task in self.discovery_tasks:
            task.cancel()
        self.discovery_tasks = set()

    # Serial data parser
    # Good code example: https://github.com/memphi2/homie-yamaha-rs232/blob/master/src/main.cpp
    async def process_serial_data(self, data):
        line = ""

        parmre = re.compile('(.)(.)(.)(..)(..)')
        for char_byte in self.rs232.iterbytes(data):
            #self.log.debug("[RcRead] char_byte: "+ str(char_byte))

            # Reset read-buffer if device control byte or 'Start of Line' (STX) read
            if char_byte == DC1 or char_byte == DC2 or char_byte == DC3 or char_byte == DC4 or char_byte == STX:
                line = char_byte.decode("utf-8")
                continue

            # If 'End of Line' (ETX) reached -> start evaluation
            if char_byte == ETX:
                self.log.debug('[RcRead] line: >>%s<<' % (line))

                if line[0] == STX.decode("utf-8"):
                    self.log.debug('[RcRead] General Report')

                    try:
                        m = parmre.match(line)
                        if m is not None:

                            if m.group(4) in self.rc_event_list:

                                if m.group(5) in self.rc_event_list[m.group(4)]:
                                    event_data_desc = str(self.rc_event_list[m.group(
                                        4)]['_content'] + '.' + self.rc_event_list[m.group(4)][m.group(5)])
                                else:
                                    event_data_desc = str(
                                        self.rc_event_list[m.group(4)]['_content'])

                                if '_object' in self.rc_event_list[m.group(4)]:
                                    self.log.info(
                                        "[RcRead] Object found for: " + event_data_desc)
                                    if m.group(5) in self.rc_event_list[m.group(4)]:
                                        await self.rc_event_list[m.group(4)]['_object'].on_rc_state_update(
                                            self.rc_event_list[m.group(4)][m.group(5)])
                                    else:
                                        await self.rc_event_list[m.group(
                                            4)]['_object'].on_rc_state_update(m.group(5))

                                else:
                                    self.log.warning(
                                        "[RcRead] No object found for: " + event_data_desc)

                            else:
                                self.log.warning(
                                    "[RcRead] Unknown event received: '" + str(m.group(4)) + "'.")
                    except NotImplementedError:
                        self.log.warning(
                            "[RcRead] Got unknown value '" + str(m.group(5)) + " for event '" + str(m.group(4)) + "'")
                    except Exception as e:
                        self.log.warning(
                            "[RcRead] Unhandled exception in loop: " + str(e))

                elif line[0] == DC1.decode("utf-8"):
                    self.log.debug('[RcRead] Unrecognised Report with DC1')
                elif line[0] == DC2.decode("utf-8"):
                    self.log.debug('[RcRead] Unrecognised Report with DC2')
                elif line[0] == DC3.decode("utf-8"):
                    self.log.debug('[RcRead] Unrecognised Report with DC3')
                elif line[0] == DC4.decode("utf-8"):
                    self.log.debug('[RcRead] Unrecognised Report with DC4')
                else:
                    self.log.warning('[RcRead] Unrecognised Report')

                # Finally, reset line
                line = ""
            else:
                line += char_byte.decode("utf-8")

    @staticmethod
    def hex_to_dec(hexstring):
        hexconv = {'0': 0, '1': 1, '2': 2, '3': 3, '4': 4, '5': 5, '6': 6, '7': 7,
                   '8': 8, '9': 9, 'A': 10, 'B': 11, 'C': 12, 'D': 13, 'E': 14, 'F': 15}
        result = hexconv[hexstring[0]] * 16 + hexconv[hexstring[1]]
        return result

    @staticmethod
    def dec_to_hex(decstring):
        return hex(decstring)


class SerialClient:

    class RS232Protocol(asyncio.Protocol):
        def __init__(self) -> None:
            super().__init__()
            self.log = logging.getLogger("serial-protocol")
            self.on_read_cb = None
            self.on_connection_made_cb = None

        def connection_made(self, transport):
            self.transport = transport
            if self.on_connection_made_cb:
                self.on_connection_made_cb()

        def data_received(self, data):
            #self.log.debug("READ: " + repr(data))
            if self.on_read_cb:
                self.on_read_cb(data)

        def connection_lost(self, exc):
            self.log.warning("Port closed.")

        def set_callbacks(self, on_read_cb, on_connection_made_cb):
            self.on_read_cb = on_read_cb
            self.on_connection_made_cb = on_connection_made_cb

    def __init__(self, loop, config, on_read_cb) -> None:
        self.log = logging.getLogger("serial")
        self.loop = loop
        self.config = config
        self.on_read_cb = on_read_cb

        self.protocol_transport = None
        self.protocol = None
        self.device_is_responing = False

    def __del__(self):
        self.terminate()

    def terminate(self):
        self.close_connection()

    async def run(self):
        self.protocol_transport, self.protocol = await serial_asyncio.create_serial_connection(
            self.loop,
            self.RS232Protocol,
            self.config.device,
            baudrate=9600,
            bytesize=8,
            parity='N',
            stopbits=1,
            timeout=1,
            xonxoff=0,
            rtscts=0
        )
        self.protocol.set_callbacks(self.on_read, self.sync_test_serial_connection)

    def close_connection(self):
        self.log.info("Closing connection...")
        if self.protocol_transport:
            self.protocol_transport.close()

    def on_read(self, data):
        if not self.device_is_responing:
            if ETX in data:
                self.device_is_responing = True
            else:
                self.log.warning(
                    "Did not receive any response. Trying again...")
        elif self.on_read_cb:
            self.loop.create_task(self.on_read_cb(data))

    async def write(self, data, format=True):
        if self.protocol_transport:
            self.log.debug("WRITE: " + str(data))
            if isinstance(data, list):
                for item in data:
                    self.protocol_transport.write(
                        self.format_command(item) if format else item)
            else:
                self.protocol_transport.write(
                    self.format_command(data) if format else data)

    def sync_test_serial_connection(self):
        return self.loop.create_task(self.test_serial_connection())

    async def test_serial_connection(self):
        self.log.info("Checking RS232 connection...")
        if not self.protocol_transport:
            return False

        connection_attemps = 0
        while not self.device_is_responing:
            await self.write(b'\x11' + "000".encode() + ETX, format=False)
            connection_attemps += 1
            if connection_attemps > 10:
                self.log.error("Could not etablish connection. Giving up...")
                raise RuntimeError("Could not etablish connection.")
            await asyncio.sleep(1)
        self.log.info("Connection is open.")

    @staticmethod
    def format_command(command):
        result = bytes()
        if not(DC1.decode("utf-8") in command or DC2.decode("utf-8") in command or DC3.decode("utf-8") in command or STX.decode("utf-8") in command or ETX.decode("utf-8") in command):
            result += STX
        return (result + command.encode() + ETX)

    @staticmethod
    def iterbytes(b):
        """Iterate over bytes, returning bytes instead of ints (python3); copied from pyserial source"""
        if isinstance(b, memoryview):
            b = b.tobytes()
        i = 0
        while True:
            a = b[i:i + 1]
            i += 1
            if a:
                yield a
            else:
                break


class MqttClient:

    class AsyncioHelper:
        def __init__(self, loop, client):
            self.log = logging.getLogger("mqtt-asyncio-helper")
            self.loop = loop
            self.client = client
            self.client.on_socket_open = self.on_socket_open
            self.client.on_socket_close = self.on_socket_close
            self.client.on_socket_register_write = self.on_socket_register_write
            self.client.on_socket_unregister_write = self.on_socket_unregister_write

        def on_socket_open(self, client, userdata, sock):
            self.log.debug("Socket opened")

            def cb():
                #self.log.debug("Socket is readable, calling loop_read")
                client.loop_read()

            self.loop.add_reader(sock, cb)
            self.misc = self.loop.create_task(self.misc_loop())

        def on_socket_close(self, client, userdata, sock):
            self.log.debug("Socket closed")
            self.loop.remove_reader(sock)
            self.misc.cancel()

        def on_socket_register_write(self, client, userdata, sock):
            self.log.debug("Watching socket for writability.")

            def cb():
                self.log.debug("Socket is writable, calling loop_write")
                client.loop_write()

            self.loop.add_writer(sock, cb)

        def on_socket_unregister_write(self, client, userdata, sock):
            self.log.debug("Stop watching socket for writability.")
            self.loop.remove_writer(sock)

        async def misc_loop(self):
            self.log.debug("misc_loop started")
            while self.client.loop_misc() == mqtt_client.MQTT_ERR_SUCCESS:
                try:
                    await asyncio.sleep(1)
                except asyncio.CancelledError:
                    break
            self.log.debug("misc_loop finished")

        async def connect(self, config):
            while True:
                try:
                    await self.loop.run_in_executor(None, self._client_connect_blocking, config)
                    break
                except (socket.gaierror, socket.timeout) as err:
                    self.log.warning(
                        "Could not resolve or reach MQTT broker. Going to try again... (Reason: %s)" % str(err))
                    await asyncio.sleep(1)
                except ConnectionError as err:
                    self.log.warning(
                        "Could not connect to MQTT broker. Going to try again... (Reason: %s)" % str(err))
                    await asyncio.sleep(1)
                except BaseException:
                    raise

        def _client_connect_blocking(self, config):
            self.client.connect(
                config.host, port=config.port, keepalive=config.keepalive)
            self.client.socket().setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 2048)

    def __init__(self, loop, config, on_connect_callback, on_disconnect_callback):
        self.log = logging.getLogger("mqtt")

        self.loop = loop
        self.config = config
        self.on_connect_callback = on_connect_callback
        self.on_disconnect_callback = on_disconnect_callback

    def __del__(self):
        self.terminate()

    def terminate(self):
        if self.client:
            self.client.disconnect()
            self.client = None

    async def run(self):
        # Set Connecting Client ID
        self.client = mqtt_client.Client(self.config.client_id)
        self.client.username_pw_set(self.config.username, self.config.password)
        self.client.reconnect_delay_set(min_delay=1, max_delay=120)
        self.client.on_connect = self.on_connect
        self.client.on_disconnect = self.on_disconnect

        # Connect to MQTT broker
        self._aioh = self.AsyncioHelper(self.loop, self.client)
        await self._aioh.connect(self.config)
    
    def on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self.log.info("Connected to MQTT broker >>%s<<" %
                          (self.config.host))
            self.loop.create_task(self.on_connect_callback())
        else:
            self.log.error("Failed to connect, return code >>%d<<", rc)

    def on_disconnect(self, client, userdata, rc):
        if rc == 0:
            self.log.info("Disconnected from MQTT broker")
        else:
            self.log.warning(
                "Unexpected disconnetion from MQTT broker, return code >>%d<<", rc)
        self.loop.create_task(self.on_disconnect_callback())

        # Trigger reconnect
        if rc != 0:
            self.loop.create_task(self._aioh.connect(self.config))

    async def publish(self, topic_state, payload, qos=0, retain=False):
        self.client.publish(topic_state, payload=payload,
                            qos=qos, retain=retain)

    async def publish_state(self, topic_state, state_new, qos=0, retain=False):
        self.log.info('Publishing state change of >>' +
                      str(topic_state) + '<< to >>' + str(state_new) + '<<.')
        await self.publish(topic_state, payload=state_new,
                            qos=qos, retain=retain)


#
# MAIN CODE
#

def sigint_handler(signal_received, frame):
    global CONTROLLER
    logging.info(
        'SIGINT or CTRL-C detected. Stopping Controller gracefully...')
    CONTROLLER.terminate()


if __name__ == '__main__':
    logging.basicConfig()
    config = Config()
    CONTROLLER = YamahaControl(config)

    # Run the sigint_handler() function when SIGINT singal is recieved
    signal(SIGINT, sigint_handler)

    # Start blocking loop
    CONTROLLER.run()
