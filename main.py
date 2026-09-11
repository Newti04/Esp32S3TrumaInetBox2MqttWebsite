main.py

import asyncio, json, machine, network, sys, time
from mqtt_as import MQTTClient, config as mqtt_config
import web_pages  # Importiert die ausgelagerten Webseiten

CONFIG_FILE = "config.json"
cfg = {}
mqtt_client = None

control_state = {"power": True, "target_room_temp": 20, "water_mode": 0, "hours": 12, "minutes": 0, "time_updated": False}
status_state = {"current_room_temp": 0.0, "water_mode_active": "OFF", "energy_mix_active": "GAS"}

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

def decode_truma_frame(frame_id, payload):
    global status_state
    if frame_id == 0x20:
        val = (payload - 30) / 2 if payload > 30 else payload
        status_state["current_room_temp"] = val
        asyncio.create_task(publish_truma_data("current_room_temp", val))

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

async def mqtt_up(client): await client.subscribe(f"{cfg['mqtt']['topic_prefix']}/set/#")

async def main():
    global mqtt_client
    is_sta = await setup_network()
    asyncio.create_task(start_webserver())
    asyncio.create_task(truma_lin_loop())
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
        except Exception as e: print("MQTT error:", e)
    while True: await asyncio.sleep(1)

try: asyncio.run(main())
except KeyboardInterrupt: print("Stopped.")


Für den stabilen Betrieb am LIN-Bus (TIN-Bus) der Truma-Heizung reicht es nicht aus, die Pins des ESP32-S3 direkt mit der Heizung zu verbinden. Da der LIN-Bus mit 12V-Pegeln arbeitet, der ESP32-S3 aber nur 3,3V verträgt, ist ein LIN-Transceiver-Baustein zwingend erforderlich.
1. Empfohlene Hardware-Komponenten
Mikrocontroller: Dein ESP32-S3 (z. B. ESP32-S3-DevKitC-1).
LIN-Transceiver-Modul: Am einfachsten und sichersten im Aufbau sind fertige Breakout-Boards mit folgenden Chips:
MCP2003 oder MCP2004 (sehr verbreitet, günstig und zuverlässig).
TJA1020 oder TJA1021 (ebenfalls hervorragend geeignet).
Stromversorgung: Ein hocheffizienter 12V-auf-5V-DC-DC-Abwärtswandler (Buck Converter), um den ESP32-S3 direkt aus dem 12V-Bordnetz des Wohnmobils zu versorgen.
2. Schaltplan / Verkabelung
Der LIN-Transceiver wird als Brücke zwischen der Truma-Heizung (12V-Logik) und dem ESP32-S3 (3,3V-Logik) geschaltet.
Von (Quelle)	Signal / Pin	Nach (Ziel)	Bemerkung
Truma RJ12-Buchse	12V (VBB)	DC-DC Wandler IN+ & Transceiver VBB	Versorgt die Elektronik aus der Heizung
Truma RJ12-Buchse	GND	DC-DC Wandler IN- & Gesamte Masse	Gemeinsames Masse-Potenzial
Truma RJ12-Buchse	LIN / TIN Bus	Transceiver LIN Pin	Die Datenader der Heizung
DC-DC Wandler	5V OUT+	ESP32-S3 5V / VBUS Pin	Stromversorgung für den ESP
ESP32-S3	GPIO 17 (TX)	Transceiver TXD Pin	Sende-Leitung zum Transceiver
ESP32-S3	GPIO 18 (RX)	Transceiver RXD Pin	Empfangs-Leitung vom Transceiver
ESP32-S3	3.3V OUT	Transceiver VIO / VCC	Logik-Referenzspannung (falls vorhanden)
Transceiver	CS / WAKE	ESP32-S3 3.3V (Dauer-High)	Aktiviert den Transceiver-Chip
Wichtiger Hinweis zum RJ12-Stecker: Die Truma CP Plus nutzt einen 6-poligen RJ12-Stecker. Miss vor dem Anschließen unbedingt mit einem Multimeter nach, auf welchen Pins die 12V und GND der Heizung liegen, um den ESP32-S3 nicht zu beschädigen.
3. Warum der LIN-Transceiver wichtig ist
Pegelwandlung: Er übersetzt die 12V-Signale des Busses sicher in die 3,3V, die dein ESP32-S3 verarbeiten kann.
Schutzschaltung: Er filtert Spannungsspitzen (Spikes) aus dem Bordnetz des Wohnmobils, die beim Schalten von Pumpen oder Lampen entstehen, und schützt so den Mikrocontroller vor dem Durchbrennen.
LIN-Konformität: Er sorgt für die exakten Flankensteilheiten bei der Übertragung mit 9600 Baud, die für eine fehlerfreie Kommunikation mit der CP Plus nötig sind
1. Die Pinbelegung des Truma-Kabels (TIN-Bus RJ12)
Die Truma-Heizung nutzt ein standardmäßiges, 6-poliges RJ12-Kabel (6P6C) im Straight-Format (1:1 durchbelegt). Wenn du die beiden Stecker nebeneinanderhältst, müssen die Aderfarben von links nach rechts absolut identisch sein. [1]
Die Belegung an der Truma- bzw. CP-Plus-Buchse sieht wie folgt aus:
Pin 1 (oder 6): Nicht belegt / Reserve
Pin 2: 12V (Dauerplus / VBB) – Versorgt das originale Bedienteil.
Pin 3: GND (Masse) – Das gemeinsame Minus-Potenzial des Bordnetzes.
Pin 4: LIN / TIN Bus Signal – Die Eindraht-Datenleitung.
Pin 5: GND (Masse) – Häufig zur Absicherung doppelt auf Masse gelegt. [1, 2]
Sicherheitstipp: Da Truma-Kabel manchmal herstellerspezifische Aderfarben nutzen, orientiere dich nicht an den Farben, sondern immer an der Pin-Nummerierung auf dem Steckergehäuse. Miss vor der Verkabelung mit einem Multimeter nach: Zwischen den GND-Pins und dem 12V-Pin müssen ca. 12V bis 14,4V Bordspannung anliegen.
2. Empfehlung für ein MCP2003 Breakout-Board
Ein reiner MCP2003-Chip im DIP-Gehäuse benötigt zusätzliche externe Bauteile (z. B. Entstörkondensatoren und Pull-up-Widerstände). Wesentlich einfacher und sauberer klappt der Aufbau mit einem fertigen LIN-Bus Breakout-Board. [1, 2]
Gute Optionen sind:
Joy-IT / Waveshare LIN-Transceiver-Module: Diese nutzen oft den kompatiblen TJA1021 oder MCP2003, sind direkt für 3,3V-Mikrocontroller wie den ESP32-S3 ausgelegt und besitzen bereits die nötigen Schutzdioden gegen Spannungsspitzen im Wohnmobil. [1, 2]
Ebay / Amazon "LIN Bus Transceiver Module": Suchst du nach "MCP2003 LIN Board" oder "TJA1020 Breakout", findest du kleine, rote oder blaue Platinen mit Schraubklemmen für die 12V-Busseite und Stiftleisten für die ESP32-Seite.
3. Anschluss des Breakout-Boards im Detail
Die meisten MCP2003-Breakouts haben zwei Seiten. So verbindest du sie:
Controller-Seite (3,3V Logik zum ESP32-S3):
TX / TXD: Verbinde diesen Pin mit GPIO 17 des ESP32-S3.
RX / RXD: Verbinde diesen Pin mit GPIO 18 des ESP32-S3.
VCC / VIO: Verbinde diesen Pin mit dem 3.3V-Ausgang des ESP32-S3 (Wichtig für die Signalpegel!).
GND: Verbinde diesen mit dem GND des ESP32-S3.
CS / EN (Chip Select / Enable): Dieser Pin muss zwingend auf High (3,3V) gelegt werden, damit der Transceiver aufwacht und aktiv wird. [1]
LIN-Bus-Seite (12V Logik zur Heizung):
VBB / VBAT: Verbinde diesen mit Pin 2 (12V) des RJ12-Kabels.
GND: Verbinde diesen mit Pin 3 oder 5 (GND) des RJ12-Kabels.
LIN / LBUS: Verbinde diesen mit Pin 4 (LIN) des RJ12-Kabels. [1]
Wichtig für den ESP32-S3: Achte darauf, dass du dem Transceiver-Board auf der Logikseite (VCC/VIO) 3,3Vvom ESP32-S3 zuführst, damit die RX/TX-Leitungen keine 5V-Spannung an die empfindlichen GPIOs des S3 abgeben.

Mit 2 ds18b20 für innen und Außentemperatur
def get_dashboard_html(control_state, status_state):
    p_status = "AN" if control_state["power"] else "AUS"
    p_color = "#28a745" if control_state["power"] else "#dc3545"
    w_modes = ["OFF", "ECO", "HIGH", "BOOST"]
    return f"""HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\n\r\n
    <!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Truma Controller</title><style>
    body {{ font-family: Arial; text-align: center; background: #f4f4f9; padding: 20px; }}
    .container {{ max-width: 450px; margin: 0 auto; background: white; padding: 20px; border-radius: 12px; box-shadow: 0 4px 8px rgba(0,0,0,0.1); }}
    .status {{ display: inline-block; padding: 8px 15px; background: {p_color}; color: white; border-radius: 20px; font-weight: bold; margin-bottom: 15px; }}
    .temp-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin: 10px 0; }}
    .temp-val {{ font-size: 2.2rem; font-weight: bold; color: #111; }}
    .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin: 15px 0; text-align: left; }}
    .card {{ background: #f8f9fa; padding: 12px; border-radius: 8px; border: 1px solid #eee; }}
    .btn {{ display: block; width: 100%; padding: 12px; margin: 8px 0; border: none; border-radius: 6px; font-weight: bold; cursor: pointer; color: white; }}
    input, select {{ width: 100%; padding: 10px; margin: 5px 0; box-sizing: border-box; border-radius: 6px; border: 1px solid #ccc; }}
    </style></head><body><div class="container"><h1>Truma Heizung</h1><div class="status">Status: {p_status}</div>
    <div class="temp-grid">
        <div class="card" style="text-align:center;"><div class="temp-val">{status_state["current_room_temp"]} &deg;C</div><small style="color:#666;">Raum Innen</small></div>
        <div class="card" style="text-align:center;"><div class="temp-val">{status_state["outside_temp"]} &deg;C</div><small style="color:#666;">Au&szlig;en</small></div>
    </div>
    <div class="grid"><div class="card"><small>ZIEL</small><br><b>{control_state["target_room_temp"]} &deg;C</b></div>
    <div class="card"><small>WARMWASSER</small><br><b>{w_modes[control_state["water_mode"]]}</b></div></div>
    <form method="POST" action="/control"><button type="submit" name="action" value="toggle_power" class="btn" style="background:#333;">Heizung { "AUS" if control_state["power"] else "EIN" }</button></form>
    <form method="POST" action="/control" style="text-align:left;"><label>Soll-Temp (°C):</label><input type="number" name="temp" min="5" max="30" value="{control_state["target_room_temp"]}">
    <label>Warmwasser:</label><select name="water"><option value="0" {"selected" if control_state["water_mode"]==0 else ""}>OFF</option><option value="1" {"selected" if control_state["water_mode"]==1 else ""}>ECO</option><option value="2" {"selected" if control_state["water_mode"]==2 else ""}>HIGH</option><option value="3" {"selected" if control_state["water_mode"]==3 else ""}>BOOST</option></select>
    <button type="submit" name="action" value="update_settings" class="btn" style="background:#0056b3;">Übernehmen</button></form>
    <br><a href="/config" style="color:#0056b3;text-decoration:none;">&rarr; System-Einstellungen</a></div>
    <script>setTimeout(()=>location.reload(), 5000);</script></body></html>"""

def get_settings_html(cfg):
    return f"""HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\n\r\n
    <!DOCTYPE html><html><head><meta charset="utf-8"><title>Config</title><style>body{{font-family:Arial;padding:20px;}}input{{width:100%;padding:8px;margin:5px 0;}}button{{background:#28a745;color:white;padding:10px;border:none;}}</style></head>
    <body><h1>System-WLAN & MQTT</h1><p><a href="/">&larr; Dashboard</a></p><form method="POST" action="/save">
    <label>WLAN SSID:</label><input type="text" name="wf_ssid" value="{cfg['wifi']['ssid']}"><label>WLAN Passwort:</label><input type="password" name="wf_pass" value="{cfg['wifi']['password']}">
    <label>MQTT Broker IP:</label><input type="text" name="mq_brk" value="{cfg['mqtt']['broker']}"><button type="submit">Speichern & Neustarten</button></form></body></html>"""
Main.py
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

async def mqtt_up(client): await client.subscribe(f"{cfg['mqtt']['topic_prefix']}/set/#")

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
        except Exception as e: print("MQTT error:", e)
    while True: await asyncio.sleep(1)

try: asyncio.run(main())
except KeyboardInterrupt: print("Stopped.")
