#!/usr/bin/env python3

import configargparse
import json
import os
import paho.mqtt.client as mqtt
import pdb
import requests
import sys
import threading
import time

from loguru import logger
from websocket import WebSocketApp 


class UnipiEvok(threading.Thread):
    def __init__(self, ip, rest_port=8080, rest_ssl=False):
        logger.info(f"Initializing Unipi EVOK on ip: {ip}")
        super().__init__()
        self.ip = ip
        self.rest_port = rest_port
        self.rest_url = ('https://' if rest_ssl else 'http://') + \
            self.ip + ':' + str(self.rest_port) + '/rest'
        self.connect_ws()
        self.start()
        self.status_update = None
        
    def connect_ws(self):
        self.get_overall_info()
        self.ws = WebSocketApp(f'ws://{self.ip}/ws', 
            on_error=self.ws_on_error,
            on_open=self.ws_on_open,
            on_message=self.ws_on_message)

    def get_rest(self, endpoint):
        logger.debug(f"Getting REST endpoint: '{endpoint}'; REST URL: '{self.rest_url}'")
        result = requests.get(f"{self.rest_url}/{endpoint}")
        return(json.loads(result.content))

    def get_overall_info(self):
        self.entities = self.get_rest('all')
        self.device_info = list(filter(lambda x: x.get('dev') in ['neuron'], self.entities))[0]
        self.model = self.device_info.get('model', 'Unknown')
        self.serial_number = str(self.device_info.get('sn', 99999))
        self.id = self.model + "-" + self.serial_number
        logger.info(f"UniPi device detected; Model: {self.model}; Serial: {self.serial_number}; ID: {self.id}")

    def run(self):
        logger.debug(f"Starting up EVOK websocket server")
        self.ws.run_forever(reconnect=5)
        
    def ws_on_open(self, ws):
        logger.info(f"Websocket opened: {ws.url}")

    def ws_on_error(self, ws, error):
        logger.error(f"Websocket error event: {error}")

    def ws_on_message(self, ws, ws_msg):
        msglist = json.loads(ws_msg)

        ### Temperature messages are coming without an outer list object, apply it to make the message handling the same
        if isinstance(msglist, dict):
            msglist = [msglist]

        for msg in msglist:
            entity_type = msg.get('dev')
            entity_id = msg.get('circuit')
            value = msg.get('value')
            logger.debug(f"UniPi update received: {self.id}/{entity_type}/{entity_id}: {value}")
            if self.status_update is not None:
                self.status_update(self.id, entity_type, entity_id, value)

    def update_entity(self, entity_type, entity_id, value):
        payload = {
            'cmd': 'set',
            'dev': entity_type,
            'circuit': entity_id,
            'value': float(value)
        }
        self.ws.send(json.dumps(payload))


class HomeAssistantMQTT:
    def __init__(self, ip, client_id = None):
        self.ip = ip        
        self.client_id = client_id
        self.devices = {}
        self.entities = {}
        self.topics = []
        self.ha_mqtt_prefix = 'homeassistant'
        self.on_entity_set = None
        self.connect()
        
    def connect(self):
        logger.info(f"Connecting to MQTT server on ip: {self.ip}")

        if self.client_id not in [None, '']:
            self.mqtt = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.client_id, clean_session=True)
        else:
            self.mqtt = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, clean_session=True)
        self.mqtt.on_subscribe = self.mqtt_subscribe
        self.mqtt.on_connect = self.mqtt_connect
        self.mqtt.on_message = self.mqtt_on_message
        self.mqtt.connect(self.ip)
        self.mqtt.loop_start()

    def mqtt_subscribe(self, client, userdata, mid, reason_code_list, properties):
        logger.debug(f"MQTT subscribe {client};{userdata};{mid};{reason_code_list};{properties}")
        
    def mqtt_connect(self, client, userdata, connect_flags, reason_code, properties):
        logger.debug(f"MQTT connected {properties}")
        for t in self.topics:
            logger.info(f"Re-subscribing to: {t}")
            self.mqtt.subscribe(t)

    def get_mqtt_topic(self, device_id, entity_type=None, entity_id=None):
        if entity_type and entity_id:
            topic = f"{self.ha_mqtt_prefix}/{entity_type}/{device_id}/{entity_id}"
        else:
            topic = f"{self.ha_mqtt_prefix}/+/{device_id}/+/set"
        logger.trace(f"Getting topic for {device_id}/{entity_type}/{entity_id}: {topic}")
        return(topic)

    def add_device(self, id, model, serial_number, manufacturer="UniPi"):
        listen_topic = f'{self.get_mqtt_topic(id)}/#'
        logger.info(f"Subscribing for {listen_topic}")
        self.mqtt.subscribe(listen_topic)
        self.topics.append(listen_topic)

        self.devices[id] = {
            'name': f'{manufacturer} {model}-{serial_number}',
            'identifiers': [ id ],
            'manufacturer': manufacturer,
            'model': model,
            'serial_number': serial_number
        }

    def add_entity(self, device_id, entity_type, entity_id, config=None):
        mqtt_topic = self.get_mqtt_topic(device_id, entity_type, entity_id)

        register_dict = {
            'unique_id': f'{device_id}_{entity_id}',
            'name': f'{entity_id}',
            'state_topic': f'{mqtt_topic}/state',
            'command_topic': f'{mqtt_topic}/set',
            'device': self.devices.get(device_id, {})
        }
        if config is not None:
            register_dict.update(config)
        register_payload = json.dumps(register_dict)
        self.mqtt.publish(f'{mqtt_topic}/config', payload=register_payload, qos=1, retain=True)

    def update_entity(self, device_id, entity_type, entity_id, value):
        mqtt_topic = self.get_mqtt_topic(device_id, entity_type, entity_id)
        logger.debug(f"Sending state update to HomeAssistant: {mqtt_topic}; {value}")
        self.mqtt.publish(f'{mqtt_topic}/state', payload=value, qos=1, retain=True)

    def remove_entity(self, device_id, entity_type, entity_id):
        mqtt_topic = self.get_mqtt_topic(device_id, entity_type, entity_id)
        logger.debug(f"Removing device: {device_id}/{entity_type}/{entity_id}")
        self.mqtt.publish(f'{mqtt_topic}/config', payload='', qos=1, retain=True)

    def mqtt_on_message(self, client, userdata, msg):
        topic_path = msg.topic.split('/')
        logger.debug(f"MQTT message received: {msg.topic}:{msg.payload}; {topic_path}")
        if topic_path[2] not in self.devices:
            logger.warning(f"This device is not known: {topic_path[2]}")
            return
        if topic_path[4] != 'set':
            logger.debug(f"Unknown command received: {topic_path[4]}")
        if self.on_entity_set is not None:
            self.on_entity_set(topic_path[2], topic_path[3], msg.payload)

class UnipiHomeAssistantBridge:
    def __init__(self, ha_client, unipi_clients, cleanup=False):
        logger.info("Initializing HA<->UniPi bridge...")
        self.ha = ha_client
        self.ha.on_entity_set = self.set_ha_entity
        self.unipis = {}
        self.cleanup = cleanup

        # Raw info from the REST interface
        self.entities = []
        self.unipi_entity_types = {
            'input': { 'ha_entity': 'binary_sensor',
                       'config': { 'payload_on' : '1', 'payload_off' : '0', 'initial_state' : '0' } },
            'relay': { 'ha_entity': 'switch',
                       'config': { 'payload_on' : '1','payload_off' : '0' } },            
            'ai':    { 'ha_entity': 'sensor',
                       'config': { 'device_class': 'voltage', 'state_class': 'measurement', 'unit_of_measurement': 'V' } },
            'ao':    { 'ha_entity': 'number',
                       'config': { 'min' : 0, 'max' : 10, 'step': 0.1, 'device_class': 'voltage', 'state_class': 'measurement', 'unit_of_measurement': 'V' } },            
            'led':   { 'ha_entity': 'switch',
                       'config': { 'payload_on' : '1', 'payload_off' : '0' } },                        
            'temp':  { 'ha_entity': 'sensor',
                       'config': { 'device_class': 'temperature', 'state_class': 'measurement', 'unit_of_measurement': '°C' } }
        }
        # Parsed device info for lookup in multidimensional dict:
        # devices[DEV_TYPE][CIRCUIT]
        self.entities = dict(map(lambda x: (x, {}) ,self.unipi_entity_types))
        for unipi in unipi_clients:
            self.unipis[unipi.id] = unipi
            unipi.status_update = self.update_ha_entity
            self.detect_unipi_entities(unipi)
        if self.cleanup:
            os._exit(0)

    def set_ha_entity(self, device_id, entity_id, value):
        logger.debug(f"Got update from HomeAssistant for {device_id}/{entity_id}: {value}")

        (entity_type, entity_id) = entity_id.split('_', 1)
        self.unipis[device_id].update_entity(entity_type, entity_id, value)

    def create_ha_entity(self, unipi, unipi_entity):
        unipi_entity_type = unipi_entity.get('dev')
        if unipi_entity_type not in self.unipi_entity_types:
            return
        ha_entity_type = self.unipi_entity_types[unipi_entity_type]['ha_entity']
        unipi_entity_id = unipi_entity.get('circuit', 'NONE')
        ha_entity_id = f'{unipi_entity_type}_{unipi_entity_id}'
        logger.debug(f"Detected entity: {unipi_entity_type}/{unipi_entity_id} -> {ha_entity_type}/{unipi.id}/{ha_entity_id}")
        self.entities[unipi_entity_type][unipi_entity_id] = unipi_entity
        if self.cleanup == True:
            self.ha.remove_entity(unipi.id, ha_entity_type, ha_entity_id)
        else:
            self.ha.update_entity(unipi.id, ha_entity_type, ha_entity_id, unipi_entity.get('value'))
            self.ha.add_entity(unipi.id, ha_entity_type, ha_entity_id, self.unipi_entity_types[unipi_entity_type].get('config', {}))

    def detect_unipi_entities(self, unipi):
        self.ha.add_device(unipi.id, unipi.model, unipi.serial_number)

        for entity in unipi.entities:
            self.create_ha_entity(unipi, entity)

    def update_ha_entity(self, device_id, entity_type, entity_id, value):
        if entity_type not in self.unipi_entity_types:
            return
        self.entities[entity_type][entity_id].update({'last_value': value, 'last_update': time.time()})
        self.ha.update_entity(device_id, self.unipi_entity_types[entity_type]['ha_entity'], f'{entity_type}_{entity_id}', value)


if __name__ == "__main__":
    parser = configargparse.ArgParser(
        description="UniPi <-> HomeAssistant MQTT bridge",
        default_config_files=["/etc/unipiha.conf", "./unipiha.conf"],
    )
    parser.add("-c", "--config", is_config_file=True, help="config file path")
    parser.add("-l", "--log-level", default="INFO", choices=["CRITICAL", "INFO", "DEBUG", "TRACE"])
    parser.add("--ha-ip", required=True)
    parser.add("--unipi-ip", required=True, action='append')
    parser.add("--cleanup", action='store_true')
    args = parser.parse_args()

    logger.remove(0)
    logger.add(sys.stderr, level=args.log_level)

    ha = HomeAssistantMQTT(args.ha_ip, client_id=f'UniPi2HomeAssistant_{os.urandom(8).hex()}')
    unipi = []
    for ip in args.unipi_ip:
        unipi.append(UnipiEvok(ip))
    bridge = UnipiHomeAssistantBridge(ha, unipi, args.cleanup)
    while True:
        time.sleep(1)
        #logger.debug("Sleep...")
