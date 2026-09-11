
import asyncio, json, machine, network, sys, time, onewire, ds18x20
from mqtt_as import MQTTClient, config as mqtt_config
import web_pages

CONFIG_FILE = "config.json"
cfg = {}
mqtt_client = None

control_state = {"power": True, "target_room_temp": 20, "water_mode": 0, "hours": 12, "minutes": 0, "time_updated": False}
status_state = {"current_room_temp": 0.0, "outside_temp": 0.0, "water_mode_active": "OFF", "energy_mix_active": "GAS"}

DS_PIN = machine.Pin(4)
try:
    ds_sensor = ds18x20.DS18X20(onewire.OneWire(DS_PIN))
    roms = sorted(ds_sensor.scan()) # Sortierung sorgt für feste Zuordnung
    print("DS18B20 Sensoren gefunden:", len(roms))
except Exception as e:
    roms = []
    print("DS18B20 Fehler:", e)

def load_config():
    global cfg
    try:
        with open(CONFIG_FILE, "r") as f: cfg = json.load(f)
    except Exception as e:
        cfg = {"wifi": {"ssid": "DeinWLAN", "password": "Passwort", "ap_ssid": "Truma_iNet_Fallback", "ap_password": "InsecurePassword123", "conn_timeout": 15}, "mqtt": {"broker": "192.168.178.50", "port": 1883, "user": "", "password": "", "client_id": "inetbox2mqtt_esp32s3", "topic_prefix": "truma"}, "hardware": {"uart_id": 1, "tx_pin": 17, "rx_pin": 18}}
        save_config()

def save_config():
    try:
        with open(CONFIG_FILE, "w") as f: json.dump(cfg, f)
    except Exception as e: print("Save error:", e)

load_config()

async def setup_network():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    wlan.connect(cfg['wifi']['ssid'], cfg['wifi']['password'])
    for _ in range(cfg['wifi']['conn_timeout']):
        if wlan.isconnected(): return True
        await asyncio.sleep(1)
    wlan.active(False)
    ap = network.WLAN(network.AP_IF)
    ap.active(True)
    ap.config(essid=cfg['wifi']['ap_ssid'], password=cfg['wifi']['ap_password'], authmode=network.AUTH_WPA2_PSK)
    return False

def url_decode(s):
    res = s.replace("+", " ")
    parts = res.split("%")
    if len(parts) == 1: return res
    decoded = parts
    for part in parts[1:]:
        try: decoded += chr(int(part[:2], 16)) + part[2:]
        except: decoded += "%" + part
    return decoded

async def handle_client(reader, writer):
    global cfg, control_state
    try:
        req = (await reader.readline()).decode("utf-8").split(" ")
        if len(req) < 2: return
        method, path, content_length = req, req, 0
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b"\n"): break
            if b"Content-Length:" in line: content_length = int(line.split(b":").strip())
            
        if method == "GET" and path == "/": 
            await writer.write(web_pages.get_dashboard_html(control_state, status_state))
        elif method == "GET" and path == "/config": 
            await writer.write(web_pages.get_settings_html(cfg))
        elif method == "POST" and path == "/control":
            body = (await reader.read(content_length)).decode("utf-8")
            p = {k: url_decode(v) for k, v in [pair.split("=") for pair in body.split("&") if "=" in pair]}
            if p.get("action") == "toggle_power":
                control_state["power"] = not control_state["power"]
                if not control_state["power"]: control_state["target_room_temp"], control_state["water_mode"] = 0, 0
            elif p.get("action") == "update_settings" and control_state["power"]:
                control_state["target_room_temp"] = max(5, min(30, int(p.get("temp", 20))))
                control_state["water_mode"] = max(0, min(3, int(p.get("water", 0))))
            await writer.write("HTTP/1.1 303 See Other\r\nLocation: /\r\nConnection: close\r\n\r\n")
        elif method == "POST" and path == "/save":
            body = (await reader.read(content_length)).decode("utf-8")
            p = {k: url_decode(v) for k, v in [pair.split("=") for pair in body.split("&") if "=" in pair]}
            cfg['wifi']['ssid'], cfg['wifi']['password'], cfg['mqtt']['broker'] = p.get('wf_ssid'), p.get('wf_pass'), p.get('mq_brk')
            save_config()
            await writer.write("HTTP/1.1 200 OK\r\n\r\n<h1>Gespeichert! Neustart...</h1>")
            await writer.drain()
            await asyncio.sleep(2)
            machine.reset()
    except Exception as e: print("Web error:", e)
    finally:
        try: await writer.close()
        except: pass

async def start_webserver(): await asyncio.start_server(handle_client, "0.0.0.0", 80)

def calc_lin_checksum(data):
    s = sum(data)
    while s > 255: s = (s & 0xFF) + (s >> 8)
    return (~s) & 0xFF

async def publish_truma_data(topic, value):
    if mqtt_client:
        try: await mqtt_client.publish(f"{cfg['mqtt']['topic_prefix']}/status/{topic}", str(value), retain=True)
        except: pass

async def read_ds18b20_loop():
    global status_state
    if not roms: return
    while True:
        try:
            ds_sensor.convert_temp()
            await asyncio.sleep_ms(750)
            
            # Sensor 1 (Index 0): Innen
            t_in = round(ds_sensor.read_temp(roms[0]), 1)
            status_state["current_room_temp"] = t_in
            asyncio.create_task(publish_truma_data("current_room_temp", t_in))
            
            # Sensor 2 (Index 1) falls vorhanden: Außen
            if len(roms) > 1:
                t_out = round(ds_sensor.read_temp(roms[1]), 1)
                status_state["outside_temp"] = t_out
                asyncio.create_task(publish_truma_data("outside_temp", t_out))
                
        except Exception as e: print("DS18B20 Loop error:", e)
        await asyncio.sleep(10)

def decode_truma_frame(frame_id, payload):
    pass 

async def truma_lin_loop():
    global control_state
    uart = machine.UART(cfg['hardware']['uart_id'], baudrate=9600, tx=cfg['hardware']['tx_pin'], rx=cfg['hardware']['rx_pin'], bits=8, parity=None, stop=1)
    while True:
        if uart.any() and uart.read(1) == b'\x55':
            id_byte = uart.read(1)
            if id_byte:
                frame_id = id_byte & 0x3F
                if frame_id == 0x22:
                    tx = bytearray(8)
                    tx = int(control_state["target_room_temp"]) if control_state["power"] else 0
                    tx = int(control_state["water_mode"]) if control_state["power"] else 0
                    uart.write(tx + bytes([calc_lin_checksum(tx)]))
                elif frame_id == 0x18 and control_state["time_updated"]:
                    tx = bytearray(8); tx, tx = control_state["hours"], control_state["minutes"]
                    uart.write(tx + bytes([calc_lin_checksum(tx)]))
                    control_state["time_updated"] = False
                else:
                    bn, to = 9, 0
                    while uart.any() < bn and to < 10: await asyncio.sleep_ms(2); to += 1
                    if uart.any() >= bn:
                        raw = uart.read(bn)
                        if raw[-1] == calc_lin_checksum(raw[:-1]): decode_truma_frame(frame_id, raw[:-1])
        await asyncio.sleep_ms(2)

async def mqtt_messages(client):
    global control_state
    async for topic, msg, ret in client.queue:
        t, m = topic.decode(), msg.decode()
        if "set/power" in t: control_state["power"] = m.upper() in ("ON", "TRUE", "1")
        elif "set/target_temp" in t and control_state["power"]:
            try: control_state["target_room_temp"] = max(5, min(30, int(float(m))))
            except: pass
        elif "set/water_mode" in t and control_state["power"]:
            modes = {"OFF": 0, "ECO": 1, "HIGH": 2, "BOOST": 3}
            if m.upper() in modes: control_state["water_mode"] = modes[m.upper()]

async def mqtt_up(client): 
    await client.subscribe(f"{cfg['mqtt']['topic_prefix']}/set/#")

async def main():
    global mqtt_client
    is_sta = await setup_network()
    asyncio.create_task(start_webserver())
    asyncio.create_task(truma_lin_loop())
    asyncio.create_task(read_ds18b20_loop())
    if is_sta:
        mqtt_config['server'], mqtt_config['port'] = cfg['mqtt']['broker'], cfg['mqtt']['port']
        mqtt_config['user'], mqtt_config['pass'] = cfg['mqtt']['user'], cfg['mqtt']['password']
        mqtt_config['client_id'], mqtt_config['subs_cb'] = cfg['mqtt']['client_id'], mqtt_messages
        mqtt_config['wifi_coro'] = mqtt_config['connect_coro'] = mqtt_up
        MQTTClient.SIGNAL_CONN = True
        mqtt_client = MQTTClient(mqtt_config)
        try:
            await mqtt_client.connect()
            asyncio.create_task(mqtt_messages(mqtt_client))
        except Exception as e:
            print("MQTT error:", e)
    while True:
        await asyncio.sleep(1)

try:
    asyncio.run(main())
except KeyboardInterrupt:
    print("Stopped.")
