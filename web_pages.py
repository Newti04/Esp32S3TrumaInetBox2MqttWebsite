def get_dashboard_html(control_state, status_state):
    p_status = "AN" if control_state["power"] else "AUS"
    p_color = "#28a745" if control_state["power"] else "#dc3545"
    w_modes = ["OFF", "ECO", "HIGH", "BOOST"]
    # WICHTIG: Kein Zeilenumbruch nach f"""
    return f"""HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\n\r\n<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
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
    # WICHTIG: Kein Zeilenumbruch nach f"""
    return f"""HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\n\r\n<!DOCTYPE html><html><head><meta charset="utf-8"><title>Config</title><style>body{{font-family:Arial;padding:20px;}}input{{width:100%;padding:8px;margin:5px 0;}}button{{background:#28a745;color:white;padding:10px;border:none;}}</style></head>
    <body><h1>System-WLAN & MQTT</h1><p><a href="/">&larr; Dashboard</a></p><form method="POST" action="/save">
    <label>WLAN SSID:</label><input type="text" name="wf_ssid" value="{cfg['wifi']['ssid']}"><label>WLAN Passwort:</label><input type="password" name="wf_pass" value="{cfg['wifi']['password']}">
    <label>MQTT Broker IP:</label><input type="text" name="mq_brk" value="{cfg['mqtt']['broker']}"><button type="submit">Speichern & Neustarten</button></form></body></html>"""
