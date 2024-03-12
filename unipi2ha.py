#!/usr/bin/env python3

import configargparse
import json
import paho.mqtt.client as mqtt
import pdb
import requests
import threading
import time

from loguru import logger
from websocket import WebSocketApp 


class UnipiEvok(threading.Thread):
    def __init__(self, ip, rest_port=8080, rest_ssl=False, status_update_callback=None):
        logger.debug(f"Initializing Unipi EVOK on ip: {ip}")
        super().__init__()
        self.ip = ip
        self.rest_port = rest_port
        self.rest_url = ('https://' if rest_ssl else 'http://') + \
            self.ip + ':' + str(self.rest_port) + '/rest'
        self.connect_ws()
        self.start()
        
    def connect_ws(self):
        self.get_overall_info()
        self.ws = WebSocketApp(f'ws://{self.ip}/ws', 
            on_error=self.ws_on_error,
            on_open=self.ws_on_open)

    def get_rest(self, endpoint):
        logger.debug(f"Getting REST endpoint: '{endpoint}'; REST URL: '{self.rest_url}'")
        result = requests.get(f"{self.rest_url}/{endpoint}")
        return(json.loads(result.content))

    def get_overall_info(self):
        self.devices = self.get_rest('all')
        self.device_info = list(filter(lambda x: x.get('dev') in ['neuron'], self.devices))[0]
        self.model = self.device_info.get('model', 'Unknown')
        self.serial_number = str(self.device_info.get('sn', 99999))
        self.id = self.model + "-" + self.serial_number
        logger.debug(f"Info collected from device: {self.id}")

    def run(self):
        logger.debug(f"Starting up EVOK websocket server")
        self.ws.run_forever(reconnect=5)
        
    def ws_on_open(self, ws):
        logger.info(f"Websocket opened: {ws.url}")

    def ws_on_error(self, ws, error):
        logger.error(f"Websocket error event: {error}")


class HomeAssistantMQTT:
    def __init__(self, ip):
        self.ip = ip        
        self.connect()
        
    def connect(self):
        logger.debug(f"Connecting to MQTT server on ip: {self.ip}")
        self.mqtt = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, clean_session=True)
        #self.mqtt = mqtt.Client(client_id="evok2", clean_session=False)
        print(self.mqtt)
        self.mqtt.on_subscribe = self.mqtt_subscribe
        self.mqtt.on_connect = self.mqtt_connect
        self.mqtt.connect(self.ip)
        self.mqtt.loop_start()

    def mqtt_subscribe(self, client, userdata, mid, reason_code_list, properties):
        logger.debug(f"MQTT subscribe {properties}")
        
    def mqtt_connect(self, client, userdata, connect_flags, reason_code, properties):
        logger.debug(f"MQTT connected {properties}")
    
class UnipiHomeAssistantBridge:
    def __init__(self, ha_client, unipi_client):
        logger.debug("Initializing HA<->UniPi bridge...")
        self.ha = ha_client
        self.ha.mqtt.on_message = self.mqtt_on_message
        self.unipi = unipi_client
        self.unipi.ws.on_message = self.ws_on_receive
        #self.ha_mqtt_prefix = 'TEST'
        self.ha_mqtt_prefix = 'homeassistant'

        # Raw info from the REST interface
        self.devices = []
        self.unipi_device_types = {
            'input': { 'ha_device': 'binary_sensor', 
                       'config': { 'payload_on' : '1', 'payload_off' : '0', 'initial_state' : '0' } },
            'relay': { 'ha_device': 'switch',
                       'config': { 'payload_on' : '1','payload_off' : '0' } },            
            'ai':    { 'ha_device': 'sensor',
                       'config': { 'device_class': 'voltage', 'state_class': 'measurement', 'unit_of_measurement': 'V' } },
            'ao':    { 'ha_device': 'number',
                       'config': { 'min' : 0, 'max' : 10, 'step': 0.1, 'device_class': 'voltage', 'state_class': 'measurement', 'unit_of_measurement': 'V' } },            
            'led':   { 'ha_device': 'switch',
                       'config': { 'payload_on' : '1', 'payload_off' : '0' } },                        
            'temp':  { 'ha_device': 'sensor',
                       'config': { 'device_class': 'temperature', 'state_class': 'measurement', 'unit_of_measurement': '°C' } }
        }
        # Parsed device info for lookup in multidimensional dict:
        # devices[DEV_TYPE][CIRCUIT]
        self.devices = dict(map(lambda x: (x, {}) ,self.unipi_device_types))
        self.detect_unipi_devices()
        listen_topic = self.get_mqtt_topic()
        logger.debug(f"Subscribing for {listen_topic}")
        self.ha.mqtt.subscribe(f"{listen_topic}/#")
    
    def detect_unipi_devices(self):
        for dev in self.unipi.devices:
            device_type = dev.get('dev')
            circuit_id = dev.get('circuit', 'NONE')
            if device_type not in self.unipi_device_types:
                continue
            logger.debug(f"Detected device: {device_type};{dev.get('circuit')}")
            self.devices[device_type][circuit_id] = dev
            self.create_ha_device(device_type, circuit_id, dev.get('value'))
            #self.remove_ha_device(device_type, circuit_id)

    def get_mqtt_topic(self, devtype=None, id=None):
        if devtype and id:
            topic = f"{self.ha_mqtt_prefix}/{self.unipi_device_types[devtype]['ha_device']}/{self.unipi.id}/{devtype}_{id}"
        else:
            topic = f"{self.ha_mqtt_prefix}/+/{self.unipi.id}/+/set"
        logger.debug(f"Getting topic for {devtype}/{id}: {topic}")
        return(topic)

    def create_ha_device(self, devtype, id, value):
        mqtt_topic = self.get_mqtt_topic(devtype, id)
        
        register_info = {
            'unique_id': f'{self.unipi.id}_{devtype}_{id}',
            'name': f'{devtype} {id}',
            'state_topic': f'{mqtt_topic}/state',
            'command_topic': f'{mqtt_topic}/set',
            'device': { 'name': f'Unipi {self.unipi.model}-{self.unipi.serial_number}', 'identifiers': [ self.unipi.id ], 'manufacturer': 'UniPi', 'model': self.unipi.model, 'serial_number': self.unipi.serial_number }
        }
        if self.unipi_device_types[devtype].get('config'):
            register_info.update(self.unipi_device_types[devtype]['config'])
        register_payload = json.dumps(register_info)
            
        logger.debug(f"Registering device: {self.unipi.id}_{devtype}_{id}; value: {value}")
        self.ha.mqtt.publish(f'{mqtt_topic}/config', payload=register_payload, qos=1, retain=True)
        time.sleep(0.01)
        self.ha.mqtt.publish(f'{mqtt_topic}/state', payload=value, qos=1)

    def remove_ha_device(self, devtype, id):
        mqtt_topic = self.get_mqtt_topic(devtype, id)
        logger.debug(f"Removing device: {self.unipi.id}_{devtype}_{id}")
        self.ha.mqtt.publish(f'{mqtt_topic}/config', payload='', qos=1, retain=True)

    def mqtt_on_message(self, client, userdata, msg):
        topic_path = msg.topic.split('/')
        logger.debug(f"MQTT message received: {msg.topic}:{msg.payload}; {topic_path}")
        if topic_path[2] != self.unipi.id:
            logger.debug(f"Uknown UniPi ID received: {topic_path[2]} != {self.unipi.id}")
        if topic_path[4] != 'set':
            logger.debug(f"Unknown command received: {topic_path[4]}")
        (dev, circuit) = topic_path[3].split('_', 1)
        
        payload = { 'cmd': 'set', 'dev': dev, 'circuit': circuit, 'value': float(msg.payload) }
        self.unipi.ws.send(json.dumps(payload))

    def ws_on_receive(self, ws, ws_msg):
        msglist = json.loads(ws_msg)

        ### Temperature messages are coming without an outer list object, apply it to make the message handling the same
        if isinstance(msglist, dict):
            msglist = [msglist]

        for msg in msglist:
            devtype = msg.get('dev')
            if devtype not in self.unipi_device_types:
                continue
            id = msg.get('circuit')
            value = msg.get('value')
            logger.debug(f"Message received: {devtype}/{id}: {value}")
            self.devices[devtype][id].update({'last_value': value, 'last_update': time.time()})
            mqtt_topic = self.get_mqtt_topic(devtype, id)
            self.ha.mqtt.publish(f'{mqtt_topic}/state', payload=value, qos=1)
        
if __name__ == "__main__":
    parser = configargparse.ArgParser(
        description="UniPi <-> HomeAssistant MQTT bridge",
        default_config_files=["/etc/unipiha.conf", "./unipiha.conf"],
    )
    parser.add("-c", "--config", is_config_file=True, help="config file path")
    parser.add("-l", "--log-level", default="INFO", choices=["CRITICAL", "INFO", "DEBUG", "TRACE"])
    parser.add("--ha-ip", required=True)
    parser.add("--unipi-ip", required=True)
    args = parser.parse_args()

    ha = HomeAssistantMQTT(args.ha_ip)
    #unipi = UnipiEvok('192.168.88.50')
    unipi = UnipiEvok(args.unipi_ip)
    bridge = UnipiHomeAssistantBridge(ha, unipi)
    #unipi.start()
    while True:
        time.sleep(1)
        logger.debug("Sleep...")
