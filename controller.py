
#
# YamahaControl App
# Control a Yamaha RX-V1500 receiver connected via RS232 using MQTT
#

import logging
import time
import re
import threading
import socket
from signal import signal, SIGINT
from types import SimpleNamespace

# Install packages for RS232 and MQTT connection, if required
try:
  import yaml
  import serial
  from paho.mqtt import client as mqtt_client
except:
  print("Installing python package dependencies...")
  pip_install = subprocess.Popen(["pip3", "install", "-r", "requirements.txt"])
  pip_install.communicate()
  import yaml
  import serial
  from paho.mqtt import client as mqtt_client


STX = b'\x02'  # Start of Text
ETX = b'\x03'  # End of Text
DC1 = b'\x11'  # Device Control 1
DC2 = b'\x12'  # Device Control 2
DC3 = b'\x13'  # Device Control 3
DC4 = b'\x14'  # Device Control 4
DEL = b'\x7F'  # Delete


def __default(obj, attr, default):
  if not hasattr(obj, attr):
    setattr(obj, attr, default)


class Config:
  """Class for parsing `config.yaml`."""

  def __init__(self, file='config.yaml'):
    """Initialize Config class and read config file."""
    logging.debug("Reading %s", file)
    try:
      with open(file, 'r') as filehandle:
        config = yaml.load(filehandle, Loader=yaml.SafeLoader)
        if "logging" in config:
          logging.config.dictConfig(config["logging"])
        self._parse_mqtt(config)
        self._parse_serial(config)
        self._parse_limits(config)
        
    except FileNotFoundError as ex:
      logging.error("Configuration file %s not found: %s", file, ex)
      exit(ex.errno)

  def _parse_mqtt(self, config):
    if not "mqtt" in config:
      logging.error("MQTT configuration not found in configuration file.")
      exit(1)
    self.mqtt = SimpleNamespace(**config["mqtt"])
    
    if not hasattr(self.mqtt, "host"):
      raise ValueError("MQTT broker not defined!")

    __default(self.mqtt, "client_id", "rxv1500-mqtt")
    __default(self.mqtt, "topic_prefix", "")
    __default(self.mqtt, "port", 1883)
    __default(self.mqtt, "username", "")
    __default(self.mqtt, "password", "")
    __default(self.mqtt, "qos", 0)
    __default(self.mqtt, "retain", False)
    __default(self.mqtt, "keepalive", 60)
  
  def _parse_serial(self, config):
    self.serial = SimpleNamespace(**config["serial"] if "serial" in config else {})
    __default(self.serial, "device", "/dev/ttyUSB0")
  
  def _parse_limits(self, config):
    self.limits = SimpleNamespace(**config["limits"] if "limits" in config else {})
    __default(self.limits, "volume", -20) # or None


class YamahaControl:
  """
  This is the main class of the Controller. __init__() will initialize all components.
  """

  class RemoteEventSubscription:
    def __init__(self, controller, topic_extension, on_event_callback, state_only=False):
      self.controller = controller
      self.topic_state = str("%s/%s/%s" % (CONFIG.mqtt.topic_prefix, CONFIG.mqtt.client_id, topic_extension))
      self.on_event_callback = on_event_callback
      self.state_only = state_only

      # functions alias
      self.publish_state = self.controller.mqtt.publish_state

    def subscribe(self):
      self.controller.log.info("Publishing states for topic: %s" % str(self.topic_state))

      # Subscribe for command topic
      if not self.state_only:
        self.topic_command = str("%s/%s" % (self.topic_state, "set"))
        self.controller.mqtt.handle.subscribe(self.topic_command, qos=1)
        self.controller.mqtt.handle.message_callback_add(self.topic_command, self.on_event_callback)
        self.controller.log.info("Registered listener for topic: %s" % str(self.topic_command))
      else:
        self.topic_command = None

    def unsubscribe(self):
      if self.controller.mqtt and self.topic_command:
        self.controller.mqtt.handle.message_callback_remove(self.topic_command)
        self.controller.mqtt.handle.unsubscribe(self.topic_command)
        self.controller.log.info("Removed listener for topic: %s" % str(self.topic_command))

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
      self.name = topic_extension
      self.controller = controller
      
      self.subscription = YamahaControl.RemoteEventSubscription(self.controller, topic_extension, self.on_mqtt_cmd_for_rc, state_only)
      self.controller.remote_subscriptions.add(self.subscription)

    def __str__(self):
      return str("Entity: %s" % self.name)

    def on_rc_state_update(self, state):
      self.controller.log.info("[RcState] State: " + str(state))
      if state in self.options and len(self.options[state]) > 0:
        self.controller.log.info("[RcState] Going to publish state >>%s<< for topic >>%s<<" % (str(self.options[state][0]), self.subscription.topic_state))

        # Publish new receiver state with mqtt
        self.subscription.publish_state(self.subscription.topic_state, self.options[state][0], retain=True)
      else:
        raise NotImplementedError

    def write_rc(self, state):
      for opt in self.options.values():
        if opt[0] == state:
          if opt[1] is not None:
            self.controller.log.info('[WriteRc] Setting >>' + str(self) + '<< (' + str(self.subscription.topic_state) + ') to >>' + str(state) + '<<.')
            self.controller.rs232.write(opt[1])
          return
      raise NotImplementedError
    
    def on_mqtt_cmd_for_rc(self, mqtt_client, userdata, message): #event_name, data, kwargs):
      #self.controller.log.debug("[MqttMessage] EVENT: " + str(message.topic) + " with " + str(userdata))
      state_new = message.payload.decode("utf-8")
      
      self.controller.log.info("[MqttMessage] Processing command >>" + str(message.topic) + "<< with data >>" + str(state_new) + "<<")
      self.write_rc(state_new)


  class GenericSensorEntity(EntityBase):
    name = ""
    def __init__(self, controller, topic_extension, entity_name):
      self.name = entity_name
      super().__init__(controller, topic_extension, state_only=True)

    def __str__(self):
      return self.name
    
    def on_rc_state_update(self, state):
      self.subscription.publish_state(self.subscription.topic_state, state, retain=True)

    def write_rc(self, state):
      self.controller.log.info("ERROR! Write on GenericSensor is not allowed! Tried: %s" % (state))


  class PowerEntity(EntityBase):
    options = {
      # Yamaha-ID: (HASS-State, Set-Cmd)
      'On': ('on', '07a1d'),  # Power On
      'Off': ('off', '07a1e') # Power Off
    }
    def write_rc(self, state):
      if state == 'on':
        self.controller.log.info('[WriteRc] Setting ' + str(self) + ' (' + str(self.subscription.topic_state) + ') to "' + str(state) + '".')
        self.controller.rs232.write(DC1.decode("utf-8") + "000")
        self.controller.rs232.write("20000")
        self.controller.rs232.write("20100")
        self.controller.rs232.write(self.options['On'][1])
        return
      elif state == 'off':
        self.controller.log.info('[WriteRc] Setting ' + str(self) + ' (' + str(self.subscription.topic_state) + ') to "' + str(state) + '".')
        self.controller.rs232.write(self.options['Off'][1])
        return
      else:
        raise NotImplementedError

    def __str__(self):
      return str("Power")
  
  class ResetEntity(EntityBase):
    options = {
      # Yamaha-ID: (HASS-State, Set-Cmd)
      'On': ('on', "%s%s%s%s" % (DC3.decode("utf-8"), DEL.decode("utf-8"), DEL.decode("utf-8"), DEL.decode("utf-8"))),  # Reset all RS232 controlled settings # (DC3, DEL, DEL, DEL, ETX)
      'Off': ('off', None)
    }

    def __str__(self):
      return str("Reset")

  class PowerZone1Entity(EntityBase):
    options = {
      # Yamaha-ID: (HASS-State, Set-Cmd)
      'On': ('on', '07e7e'),  # Power On
      'Off': ('off', '07e7f') # Power Off
    }
    def __str__(self):
      return str("PowerZone1")


  class PowerZone2Entity(EntityBase):
    options = {
      # Yamaha-ID: (HASS-State, Set-Cmd)
      'On': ('on', '07eba'),  # Power On
      'Off': ('off', '07ebb') # Power Off
    }
    def __str__(self):
      return str("PowerZone2")


  class SpeakersAEntity(EntityBase):
    options = {
      # Yamaha-ID: (HASS-State, Set-Cmd)
      'On': ('on', '07eab'),  # Power On
      'Off': ('off', '07eac') # Power Off
    }
    def __str__(self):
      return str("SpeakersA")

  
  class SpeakersBEntity(EntityBase):
    options = {
      # Yamaha-ID: (HASS-State, Set-Cmd)
      'On': ('on', '07ead'),  # Power On
      'Off': ('off', '07eae') # Power Off
    }
    def __str__(self):
      return str("SpeakersB")


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
    def __str__(self):
      return str("InputSource")

  
  class InputModeEntity(EntityBase):
    options = {
      # Yamaha-ID: (HASS-State, Set-Cmd)
      'Auto': ('auto', '07ea6'),
      'DTS': ('dts', '07ea8'),
      'Analog': ('analog', '07eaa'),
      'AnalogOnly': ('analog', '')
    }
    def __str__(self):
      return str("InputMode")


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
    def __str__(self):
      return str("TunerPreset")

    def write_rc(self, state):
      # Format in HA: '2.0' (string) -> only use '2'
      if len(state) > 0:        
        super().write_rc(state[0])

  class OsdEntity(EntityBase):
    options = {
      # Yamaha-ID: (HASS-State, Set-Cmd)
      'Off': ('off', '07eb0'),
      'Short': ('short', '07eb1'),
      'Full': ('full', '07eb2'),
    }
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
    def __str__(self):
      return str("DSP")


  class MasterVolumeEntity(EntityBase):    
    def __str__(self):
      return str("MasterVolume")

    def on_rc_state_update(self, vol_str):
      # Process volume from hex string to -dB
      step = 0.5
      new_db_val = (YamahaControl.hex_to_dec(vol_str) * step) - 99.5

      # Update state
      self.subscription.publish_state(self.subscription.topic_state, new_db_val, retain=True)

    def write_rc(self, vol_new):
      if CONFIG.limits.volume is not None and float(vol_new) > CONFIG.limits.volume:
        vol_new = float(CONFIG.limits.volume)
        self.controller.log.info("[V] Volume limit reached. Changed value to: %d" % (vol_new))
      
      # Process volume from -dB string to hex
      step = 0.5
      new_normed = int((float(vol_new) + 99.5) / step)
      raw_db_val = str(str(YamahaControl.dec_to_hex(new_normed))[2:]).upper()
      self.controller.log.debug("[V] Volume to set: %s" % str(raw_db_val))
      self.controller.rs232.write("230" + str(raw_db_val))

  
  class MuteEntity(EntityBase):
    options = {
      # Yamaha-ID: (HASS-State, Set-Cmd)
      'On': ('on', '07ea2'),  # Mute
      'Off': ('off', '07ea3') # Unmute
    }
    def __str__(self):
      return str("Mute")


  def __init__(self):
    """
    ENTRYPOINT / INIT of YamahaControl / Controller
    """

    # Setup logging
    self.log = logging.getLogger("Controller")
    self.log.info("Starting YamahaControl...")
    
    # Setup physical device access via serial connection
    self.rs232 = RS232Client(CONFIG.serial.device)

    # Setup MQTT client
    # Callback: Generate rc event list which registers MQTT subscriptions
    self.remote_subscriptions = set()
    self.rc_event_list = None
    self.mqtt = MqttClient(self._setup_rc_event_list, self._clear_remote_subscriptions)
    
    # start thread
    self.serialReaderEnabled = True
    self.serialReadThread = threading.Thread(target=self.reader)
    self.serialReadThread.start()

  def __del__(self):
    """
    Destructor of Controller
    """
    self._clear_remote_subscriptions()
    self.terminate()
  
  def terminate(self):
    if self.mqtt:
      self.log.info("Terminating MQTT connection...")
      self.mqtt.terminate()
    self.mqtt = None

    if self.rs232:
      self.log.info("Terminating RS232 connection...")
      self.serialReaderEnabled = False
      self.serialReadThread.join()

      self.rs232.terminate()
    self.rs232 = None

  def _setup_rc_event_list(self):
    # Define possible events/messages from RX-V1500 and initialize Entity Objects
    self.rc_event_list = {
      '00':{
        '_content':'NoGuard',
        '00':'Ok',
        '01':'Busy',
        '02':'PowerOff'
        },
      '01':{
        '_content':'Warning',
        '00':'OverCurrent',
        '01':'DCDetect',
        '02':'PowerTrouble',
        '03':'OverHeat'
        },
      '10':{
        '_content':'Format',
        '_object': YamahaControl.GenericSensorEntity(self, 'playback-format', 'PlaybackFormat'),
        '00':'ExternalDecoder',
        '01':'Analog',
        '02':'PCM',
        '03':'DD',
        '04':'DD20',
        '05':'DDKaraoke',
        '06':'DD61',
        '07':'DTS',
        '08':'DTSES',
        '09':'Digital'
        },
      '11':{
        '_content':'Fs',
        '_object': YamahaControl.GenericSensorEntity(self, 'playback-bitrate', 'PlaybackBitrate'),
        '00':'Analog',
        '01':'32kHz',
        '02':'441kHz',
        '03':'48kHz',
        '04':'64kHz',
        '05':'882kHz',
        '06':'96kHz',
        '07':'Unknown'
        },
      '12':{
        '_content':'61ES',
        '00':'On',
        '01':'Off'
        },
      '13':{
        '_content':'ThrBypass',
        '00':'Normal',
        '01':'Bypass'
        },
      '14':{
        '_content':'REDDTS',
        '00':'Release',
        '01':'Wait'
        },
      '15':{
        '_content':'Tuner',
        '00':'NotTuned',
        '01':'Tuned'
        },
      '20':{
        '_content':'Power',
        '_object': YamahaControl.PowerEntity(self, 'power'),
        '00':'Off',
        '01':'On'
        },
      '21':{
        '_content':'Input',
        '_object': YamahaControl.InputSourceEntity(self, 'input-source'),
        # 0x is for 6ch off, 1x is for 6ch on
        '00':'Phono',
        '01':'CD',
        '02':'Tuner',
        '03':'CDR',
        '04':'MDTape',
        '05':'DVD',
        '06':'DTVLD',
        '07':'Cable',
        '09':'VCR1',
        '0A':'VCR2',
        '0B':'VAux',
        '10':'Phono',
        '11':'CD',
        '12':'Tuner',
        '13':'CDR',
        '14':'MDTape',
        '15':'DVD',
        '16':'DTVLD',
        '17':'Cable',
        '19':'VCR1',
        '1A':'VCR2',
        '1B':'VAux'
        },
      '22':{
        '_content':'InputMode',
        '_object': YamahaControl.InputModeEntity(self, 'input-mode'),
        '00':'Auto',
        '02':'DTS',
        '04':'Analog',
        '05':'AnalogOnly'
        },
      '23':{
        '_content':'Mute',
        '_object': YamahaControl.MuteEntity(self, 'mute'),
        '00':'Off',
        '01':'On'
        },
      '24':{
        '_content':'Zone2Input',
        '00':'Phono',
        '01':'CD',
        '02':'Tuner',
        '03':'CDR',
        '04':'MDTape',
        '05':'DVD',
        '06':'DTVLD',
        '07':'Cable',
        '09':'VCR1',
        '0A':'VCR2',
        '0B':'VAux'
        },
      '25':{
        '_content':'Zone2Mute',
        '_object': YamahaControl.PowerZone2Entity(self, 'power-zone-2'), 
        '00':'Off',
        '01':'On'
        },
      '26':{
        '_content':'MasterVolume',
        '_object': YamahaControl.MasterVolumeEntity(self, 'output-volume')
        },
      '28':{
        '_content':'DSP',
        '_object': YamahaControl.DspEntity(self, 'dsp'),
        '85':'straight',
        '05':'concert hall',
        '0E':'jazz club',
        '10':'rock concert',
        '14':'disco',
        '16':'game',
        '18':'music video',
        '20':'mono movie',
        '21':'variety sports',
        '24':'spectacle',
        '25':'sci-fi',
        '28':'adventure',
        '29':'general',
        '36':'thx cinema',
        '2C':'pro logic sur standard',
        '2D':'pro logic sur enhanced',
        '30':'pro logic II movie',
        '31':'pro logic II music',
        '38':'pro logic II game',
        '32':'dts neo 6 cinema',
        '33':'dts neo 6 music',
        '34':'2ch stereo',
        '35':'direct stereo',
        '17':'7ch stereo',
        },
      '29':{
        '_content':'TunerPage',
        '00':'A',
        '01':'B',
        '02':'C',
        '03':'D',
        '04':'E'
        },
      '2A':{
        '_content':'TunerPreset',
        '_object': YamahaControl.TunerPresetEntity(self, 'input-tuner-preset'),
        '00':'1',
        '01':'2',
        '02':'3',
        '03':'4',
        '04':'5',
        '05':'6',
        '06':'7',
        '07':'8'
        },
      '2B':{
        '_content':'OSD',
        '_object': YamahaControl.OsdEntity(self, 'input-osd'),
        '00':'Full',
        '01':'Short',
        '02':'Off'
        },
      '2C':{
        '_content':'Sleep',
        '00':'120',
        '01':'90',
        '02':'60',
        '03':'30',
        '04':'Off'
        },
      '2D':{
        '_content':'61ESKey',
        '00':'Off',
        '01':'On'
        },
      '2E':{
        '_content':'SpkrRelayA',
        '_object': YamahaControl.SpeakersAEntity(self, 'speakers-a'),
        '00':'Off',
        '01':'On'
        },
      '2F':{
        '_content':'SpkrRelayB',
        '_object': YamahaControl.SpeakersBEntity(self, 'speakers-b'),
        '00':'Off',
        '01':'On'
        },
      '30':{
        '_content':'HomeBank',
        '00':'Main',
        '01':'A',
        '02':'B',
        '03':'C'
        },
      '31':{
        '_content':'HomePreset',
        '00':'A',
        '01':'B',
        '02':'C'
        },
      '32':{
        '_content':'VolumeBank',
        '00':'Main',
        '01':'A',
        '02':'B',
        '03':'C'
        },
      '33':{
        '_content':'VolumePreset',
        '00':'A',
        '01':'B',
        '02':'C'
        },
      '34':{
        '_content':'Headphone',
        '_object': YamahaControl.GenericSensorEntity(self, 'headphone', 'Headphone'),
        '00':'Off',
        '01':'On'
        },
      '35':{
        '_content':'FMAM',
        '00':'FM',
        '01':'AM'
        },
      '99': {   # ToDo: Unknown
        '_content':'Z1',
        '_object': YamahaControl.PowerZone1Entity(self, "power-zone-1")
        }
    }
  
    # Finally, create subscriptions at remote broker
    self._create_remote_subscriptions()

  def _create_remote_subscriptions(self):
    for sub in self.remote_subscriptions:
      sub.subscribe()

  def _clear_remote_subscriptions(self):
    for sub in self.remote_subscriptions:
      sub.unsubscribe()
    self.remote_subscriptions = set()

  # Serial port reader
  # Good code example: https://github.com/memphi2/homie-yamaha-rs232/blob/master/src/main.cpp
  def reader(self):
    line = ""
    
    parmre = re.compile('(.)(.)(.)(..)(..)')
    self.log.info("[RcRead] Waiting for message in read-loop...")
    while self.serialReaderEnabled:
      serial_buf = self.rs232.read(8)
      for char_byte in serial.iterbytes(serial_buf):
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
                    event_data_desc = str(self.rc_event_list[m.group(4)]['_content'] + '.' + self.rc_event_list[m.group(4)][m.group(5)])
                  else:
                    event_data_desc = str(self.rc_event_list[m.group(4)]['_content'])

                  if '_object' in self.rc_event_list[m.group(4)]:
                    self.log.info("[RcRead] Object found for: " + event_data_desc)
                    if m.group(5) in self.rc_event_list[m.group(4)]: 
                      self.rc_event_list[m.group(4)]['_object'].on_rc_state_update(self.rc_event_list[m.group(4)][m.group(5)])
                    else:
                      self.rc_event_list[m.group(4)]['_object'].on_rc_state_update(m.group(5))

                  else:
                    self.log.warning("[RcRead] No object found for: " + event_data_desc)
                  
                else:
                  self.log.warning("[RcRead] Unknown event received: '" + str(m.group(4)) + "'.")
            except NotImplementedError:
              self.log.warning("[RcRead] Got unknown value '" + str(m.group(5)) + " for event '" + str(m.group(4)) + "'")
            except Exception as e:
              self.log.warning("[RcRead] Unhandled exception in loop: " + str(e))

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
    self.log.info("[RcRead] Exit read-loop.")

  @staticmethod
  def hex_to_dec(hexstring):
    hexconv = {'0':0,'1':1, '2':2, '3':3, '4':4, '5':5, '6':6, '7':7, '8':8, '9':9, 'A':10, 'B':11, 'C':12, 'D':13, 'E':14, 'F':15}
    result = hexconv[hexstring[0]] * 16 + hexconv[hexstring[1]]
    return result

  @staticmethod
  def dec_to_hex(decstring):
    return hex(decstring)
    

class RS232Client:

  def __init__(self, device):
    self.log = logging.getLogger("RS232")
    self.conn = serial.Serial()
    self.device = device

  def __del__(self):
    self.terminate()
  
  def terminate(self):
    if self.conn:
      self.close_connection()
    self.conn = None

  def open_connection(self):
    if self.conn.isOpen() is False:
      self.conn_init = self.init_connection()
      if self.conn_init and self.conn_init.isOpen():
        self.conn = self.conn_init
        return self.conn
      else:
        return None
    else:
      return self.conn

  def close_connection(self):
    self.log.info("Closing RS232 connection...")
    if self.conn.isOpen():
      self.conn.close()


  def init_connection(self):
    self.log.info("Initializing RS232 connection...")

    ser = serial.Serial(self.device, baudrate=9600, bytesize=8, parity='N', stopbits=1, timeout=1, xonxoff=0, rtscts=0)
    ser.setDTR(1)
    ser.setRTS(1)
    
    connection_attemps = 0
    self.device_is_responing = False
    if ser.isOpen():
      response = bytes()
      while not self.device_is_responing:
        ser.write(b'\x11' + "000".encode() + ETX)
        response = ser.read(200)
        if ETX in response:
          self.device_is_responing = True
          break
        else:
          self.log.warning("Did not receive any response. Trying again...")
        connection_attemps += 1
        if connection_attemps > 10:
          self.log.error("Could not etablish connection. Giving up...")
          return None
        time.sleep(1)
      self.log.info("Connection is open.")
    else:
      return None
    return ser


  def read(self, size=0):
    ser = self.open_connection()
    #self.log.debug("READ: " + str(size))
    if size:
      return ser.read(size)
    else:
      return ser.read()
  
  def write(self, data):
    ser = self.open_connection()
    self.log.debug("WRITE: " + str(data))
    ser.write(self.format_command(data))

  def format_command(self, command):
    result = bytes()
    if not(DC1.decode("utf-8") in command or DC2.decode("utf-8") in command or DC3.decode("utf-8") in command or STX.decode("utf-8") in command or ETX.decode("utf-8") in command):
      result += STX
    return (result + command.encode() + ETX)


class MqttClient:

  def __init__(self, on_connect_callback, on_disconnect_callback):
    self.log = logging.getLogger("MQTT")

    self.on_connect_callback = on_connect_callback
    self.on_disconnect_callback = on_disconnect_callback
    self.handle = self.connect_mqtt()

  def __del__(self):
    self.terminate()
  
  def terminate(self):
    if self.handle:
      self.handle.disconnect()
      self.handle = None

  def connect_mqtt(self):
    # Set Connecting Client ID
    client = mqtt_client.Client(CONFIG.mqtt.client_id)
    client.username_pw_set(CONFIG.mqtt.username, CONFIG.mqtt.password)
    client.reconnect_delay_set(min_delay=1, max_delay=120)
    client.on_connect = self.on_connect
    client.on_disconnect = self.on_disconnect

    # Try to connect to mqtt broker
    while True:
      try:
        client.connect(CONFIG.mqtt.host, port=CONFIG.mqtt.port, keepalive=CONFIG.mqtt.keepalive)
        break
      except socket.gaierror as err:
        self.log.warning("Could not resolve or reach MQTT broker. Going to try again... (Reason: %s)" % str(err))
        time.sleep(1)
      except BaseException:
        raise
 
    return client

  def on_connect(self, client, userdata, flags, rc):
    if rc == 0:
        self.log.info("Connected to MQTT broker >>%s<<" % (CONFIG.mqtt.host))
        self.on_connect_callback()
    else:
        self.log.error("Failed to connect, return code >>%d<<", rc)

  def on_disconnect(self, client, userdata, rc):
    if rc == 0:
        self.log.info("Disconnected from MQTT broker")
    else:
        self.log.warning("Unexpected disconnetion from MQTT broker, return code >>%d<<", rc)
    self.on_disconnect_callback()

  def publish_state(self, topic_state, state_new, qos=0, retain=False):
    self.log.info('Publishing state change of >>' + str(topic_state) + '<< to >>' + str(state_new) + '<<.')
    self.handle.publish(topic_state, payload=state_new, qos=qos, retain=retain)



#
# MAIN CODE
#

def sigint_handler(signal_received, frame):
  global CONTROLLER
  logging.info('SIGINT or CTRL-C detected. Stopping Controller gracefully...')
  CONTROLLER.terminate()

if __name__ == '__main__':
  logging.basicConfig()
  CONFIG = Config()
  CONTROLLER = YamahaControl()
  
  # Run the sigint_handler() function when SIGINT singal is recieved
  signal(SIGINT, sigint_handler)
  
  # Start blocking mqtt loop
  CONTROLLER.mqtt.handle.loop_forever(retry_first_connection=True)
