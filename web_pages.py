
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
    .temp {{ font-size: 3rem; font-weight: bold; }}
    .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin: 15px 0; text-align: left; }}
    .card {{ background: #f8f9fa; padding: 12px; border-radius: 8px; border: 1px solid #eee; }}
    .btn {{ display: block; width: 100%; padding: 12px; margin: 8px 0; border: none; border-radius: 6px; font-weight: bold; cursor: pointer; color: white; }}
    input, select {{ width: 100%; padding: 10px; margin: 5px 0; box-sizing: border-box; border-radius: 6px; border: 1px solid #ccc; }}
    </style></head><body><div class="container"><h1>Truma Heizung</h1><div class="status">Status: {p_status}</div>
    <div class="temp">{status_state["current_room_temp"]} &deg;C</div><p style="color:#666;margin-top:-5px;">Raumtemperatur</p>
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
  
