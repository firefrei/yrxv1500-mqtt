# YRXV1500-MQTT
## Control your Yamaha RX-V1500 via MQTT

A python3 application that enables remote control of a Yamaha RX-V1500 A/V amplifier via MQTT. The script can run on a Raspberry Pi or any similar device, which is connected to a RX-V1500 amplivier via a RS232 serial connection. YRXV1500-MQTT connects to your exisiting MQTT broker, subscribes for command topics and publishes state changes, whenever any amplifier entity state is changed (e.g., one of the amplifier's buttons is pressed).
To simplify the setup, the application also supports MQTT auto discovery for [HomeAssistant](https://home-assistant.io).  

The script was tested on a Raspberry Pi running Raspian. Currently, not all RX-V1500 entities can be read or set.
Use it on your own risk! The tool is provided as-is, with no guarantee that the RS232 commands work or don't damage your device.

### Requirements
- Yamaha RX-V1500 A/V-Reciever
- Raspberry Pi (or similar) with RS232 interface or USB<->RS232 converter
- Python3
- Python3 library `pyyaml`, `asyncio`, `pyserial-asyncio`, and `paho-mqtt` (will be installed by this tool on startup)
  

### Usage
Easy:  
1. Clone this repository
2. Edit the `config.yaml` file according to your needs
3. Run `python3 controller.py`

As service using systemd:  
1. Same as *Easy*, but don't run manually.
2. Edit file `systemd/yrxv1500-mqtt.service` and change working directory and file path of *controller.py* to your needs (defaults to `/srv/yrxv1500-mqtt/controller.py`)
3. Link and enable systemd config:
```
sudo ln -s /srv/yrxv1500-mqtt/systemd/yrxv1500-mqtt.service /etc/systemd/system/yrxv1500-mqtt.service
sudo systemctl daemon-reload
sudo systemctl enable yrxv1500-mqtt.service
sudo systemctl start yrxv1500-mqtt.service
```
4. Verify service status `sudo systemctl status yrxv1500-mqtt.service`


## MQTT topics
### Syntax
- State topics: `<PREFIX>/<IDENTIFIER>/<ENTITY>`  
- Command topics: `<PREFIX>/<IDENTIFIER>/<ENTITY>/set`
- Discovery topics: `<PREFIX>/<DEVICE_CLASS>/<UNIQUE_ID>/<ENTITY>`

### Supported control and sensor entities
- `<PREFIX>/<IDENTIFIER>/playback-format`
- `<PREFIX>/<IDENTIFIER>/playback-bitrate`
- `<PREFIX>/<IDENTIFIER>/power`
- `<PREFIX>/<IDENTIFIER>/power/set`
- `<PREFIX>/<IDENTIFIER>/input-source`
- `<PREFIX>/<IDENTIFIER>/input-source/set`
- `<PREFIX>/<IDENTIFIER>/input-mode`
- `<PREFIX>/<IDENTIFIER>/input-mode/set`
- `<PREFIX>/<IDENTIFIER>/mute`
- `<PREFIX>/<IDENTIFIER>/mute/set`
- `<PREFIX>/<IDENTIFIER>/power-zone-2`
- `<PREFIX>/<IDENTIFIER>/power-zone-2/set`
- `<PREFIX>/<IDENTIFIER>/output-volume`
- `<PREFIX>/<IDENTIFIER>/output-volume/set`
- `<PREFIX>/<IDENTIFIER>/dsp`
- `<PREFIX>/<IDENTIFIER>/dsp/set`
- `<PREFIX>/<IDENTIFIER>/input-tuner-preset`
- `<PREFIX>/<IDENTIFIER>/input-tuner-preset/set`
- `<PREFIX>/<IDENTIFIER>/input-osd`
- `<PREFIX>/<IDENTIFIER>/input-osd/set`
- `<PREFIX>/<IDENTIFIER>/speakers-a`
- `<PREFIX>/<IDENTIFIER>/speakers-a/set`
- `<PREFIX>/<IDENTIFIER>/speakers-b`
- `<PREFIX>/<IDENTIFIER>/speakers-b/set`
- `<PREFIX>/<IDENTIFIER>/headphone`
- `<PREFIX>/<IDENTIFIER>/power-zone-1`
- `<PREFIX>/<IDENTIFIER>/power-zone-1/set `


## Integration into HomeAssistant
MQTT enables easy integration in any HomeAssistant instance, which makes automation and remote control even smarter.
Simply configure MQTT in HomeAssistant and enable MQTT auto discovery.  

*Note*: Minimum HomeAssistant **Version 2021.6** required!  
