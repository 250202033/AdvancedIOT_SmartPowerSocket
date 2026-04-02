import sqlite3
import json
import time
import threading
from datetime import datetime, timedelta
from flask import Flask, render_template_string, request, redirect, url_for
import paho.mqtt.client as mqtt

# --- CONFIGURATION ---
MQTT_BROKER = "localhost" 
MQTT_PORT = 1883
DB_NAME = "iot_sockets.db"

app = Flask(__name__)

# --- DATABASE SETUP ---
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS sockets (
                    socket_id TEXT PRIMARY KEY, 
                    relay_state TEXT DEFAULT 'OFF', 
                    fault_state TEXT DEFAULT '{}', 
                    telemetry TEXT DEFAULT '{}', 
                    last_seen DATETIME
                )''')
    # Expanded schedules table to support recurring rules
    c.execute('''CREATE TABLE IF NOT EXISTS schedules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, 
                    socket_id TEXT, 
                    sched_type TEXT, 
                    on_epoch INTEGER, 
                    off_epoch INTEGER,
                    time_on TEXT,
                    time_off TEXT,
                    days TEXT,
                    last_dispatched_off INTEGER,
                    is_active INTEGER DEFAULT 1,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )''')
    conn.commit()
    conn.close()

# --- MQTT SETUP ---
def on_connect(client, userdata, flags, rc):
    print(f"Connected to MQTT broker with result code {rc}")
    client.subscribe("telemetry/+/pzem")
    client.subscribe("state/+/+")

def on_message(client, userdata, msg):
    topic = msg.topic
    payload = msg.payload.decode('utf-8')
    parts = topic.split('/')
    
    if len(parts) < 3: return
    category, socket_id, subtopic = parts[0], parts[1], parts[2]

    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO sockets (socket_id) VALUES (?)", (socket_id,))

    if category == 'telemetry' and subtopic == 'pzem':
        c.execute("UPDATE sockets SET telemetry=?, last_seen=CURRENT_TIMESTAMP WHERE socket_id=?", (payload, socket_id))
    elif category == 'state' and subtopic == 'relay':
        try:
            state_data = json.loads(payload)
            c.execute("UPDATE sockets SET relay_state=?, last_seen=CURRENT_TIMESTAMP WHERE socket_id=?", (state_data.get("state"), socket_id))
        except json.JSONDecodeError:
            pass
    elif category == 'state' and subtopic == 'fault':
        c.execute("UPDATE sockets SET fault_state=?, last_seen=CURRENT_TIMESTAMP WHERE socket_id=?", (payload, socket_id))
    
    conn.commit()
    conn.close()

mqtt_client = mqtt.Client()
mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message
mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
mqtt_client.loop_start()

# --- BACKGROUND SCHEDULE MANAGER ---
def calculate_timer_window(time_on_str, time_off_str, days_list):
    now = datetime.now()
    on_hour, on_minute = map(int, time_on_str.split(':'))
    off_hour, off_minute = map(int, time_off_str.split(':'))
    
    for i in range(-1, 8):
        check_date = now + timedelta(days=i)
        
        if check_date.weekday() in days_list:
            on_dt = check_date.replace(hour=on_hour, minute=on_minute, second=0, microsecond=0)
            off_dt = check_date.replace(hour=off_hour, minute=off_minute, second=0, microsecond=0)
            
            if off_dt <= on_dt:
                off_dt += timedelta(days=1)
                
            if off_dt > now:
                return int(on_dt.timestamp()), int(off_dt.timestamp())
                
    return None, None

def background_scheduler():
    while True:
        try:
            conn = sqlite3.connect(DB_NAME)
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            now_epoch = int(time.time())
            
            c.execute("SELECT * FROM schedules WHERE sched_type='recurring' AND is_active=1 AND (last_dispatched_off IS NULL OR last_dispatched_off <= ?)", (now_epoch,))
            rows = c.fetchall()
            
            for row in rows:
                r = dict(row)
                days_list = [int(d) for d in r['days'].split(',')]
                on_epoch, off_epoch = calculate_timer_window(r['time_on'], r['time_off'], days_list)
                
                if on_epoch and off_epoch:
                    c.execute("UPDATE schedules SET last_dispatched_off=? WHERE id=?", (off_epoch, r['id']))
                    conn.commit()
                    
                    if on_epoch <= int(time.time()):
                        on_epoch = int(time.time()) + 15
                    payload = f"{on_epoch},{off_epoch}"
                    mqtt_client.publish(f"cmd/{r['socket_id']}/timer", payload)
                    print(f"[{datetime.now()}] Re-armed recurring schedule for {r['socket_id']}: {payload}")
                    
            conn.close()
        except Exception as e:
            print(f"Background Scheduler Error: {e}")
            
        time.sleep(30) 

scheduler_thread = threading.Thread(target=background_scheduler, daemon=True)
scheduler_thread.start()

# --- HTML TEMPLATE ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Smart Socket Dashboard</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.10.5/font/bootstrap-icons.css">
    <style>
        body { background-color: #f4f6f9; }
        .card { border-radius: 12px; border: none; }
        .metric-box { background: #fff; border: 1px solid #e9ecef; border-radius: 8px; padding: 10px; text-align: center; }
        .metric-val { font-size: 1.1rem; font-weight: 600; margin-bottom: 0; color: #343a40; }
        .metric-label { font-size: 0.75rem; color: #6c757d; text-transform: uppercase; letter-spacing: 0.5px; }
        .day-checkbox { display: none; }
        .day-label { border: 1px solid #dee2e6; border-radius: 4px; padding: 4px 8px; cursor: pointer; font-size: 0.8rem; user-select: none; }
        .day-checkbox:checked + .day-label { background-color: #0d6efd; color: white; border-color: #0d6efd; }
        .fault-active { border: 2px solid #dc3545 !important; box-shadow: 0 0 15px rgba(220, 53, 69, 0.2); }
    </style>
</head>
<body>
<div class="container py-4">
    <div class="d-flex flex-column flex-md-row justify-content-between align-items-md-center mb-4 gap-2">
        <h2 class="mb-0 fw-bold text-dark"><i class="bi bi-lightning-charge-fill text-warning"></i> Socket Dashboard</h2>
        <span id="refresh-status" class="badge bg-primary px-3 py-2 rounded-pill shadow-sm" style="font-size: 0.85rem;">
            <i class="bi bi-arrow-repeat"></i> Auto-refreshing (10s)
        </span>
    </div>
    
    <div class="row g-4">
        {% for s in sockets %}
        {% set has_fault = (s.fault_state.get('fault') == True and s.fault_state.get('reason', 'NONE') != 'NONE') %}
        
        <div class="col-12 col-xl-6">
            <div class="card shadow-sm h-100 {% if has_fault %}fault-active{% endif %}">
                <div class="card-header bg-white border-bottom-0 pt-3 pb-0 d-flex justify-content-between align-items-center">
                    <h5 class="mb-0 fw-bold text-secondary"><i class="bi bi-outlet"></i> {{ s.socket_id }}</h5>
                    {% if has_fault %}
                        <span class="badge bg-danger px-3 py-2 rounded-pill"><i class="bi bi-exclamation-triangle-fill"></i> FAULTED</span>
                    {% elif s.relay_state == 'ON' %}
                        <span class="badge bg-success px-3 py-2 rounded-pill"><i class="bi bi-power"></i> ON</span>
                    {% else %}
                        <span class="badge bg-secondary px-3 py-2 rounded-pill"><i class="bi bi-power"></i> OFF</span>
                    {% endif %}
                </div>
                
                <div class="card-body">
                    {% if has_fault %}
                    <div class="alert alert-danger d-flex flex-column flex-sm-row justify-content-between align-items-sm-center mb-3 p-3 rounded-3 border-danger">
                        <div class="mb-2 mb-sm-0 text-danger fw-bold">
                            <i class="bi bi-shield-x fs-5 me-2 align-middle"></i>
                            Protection Tripped: <span class="text-dark">{{ s.fault_state.get('reason') }}</span>
                        </div>
                        <form action="/cmd" method="POST" class="m-0">
                            <input type="hidden" name="socket_id" value="{{ s.socket_id }}">
                            <button type="submit" name="action" value="RESET" class="btn btn-danger btn-sm w-100 interactive-el fw-bold shadow-sm">
                                <i class="bi bi-arrow-clockwise"></i> Clear & Reset
                            </button>
                        </form>
                    </div>
                    {% endif %}

                    <div class="row g-2 mb-3">
                        <div class="col-6 col-sm-3"><div class="metric-box"><div class="metric-label">Voltage</div><div class="metric-val">{{ s.telemetry.get('voltage', 0) }} V</div></div></div>
                        <div class="col-6 col-sm-3"><div class="metric-box"><div class="metric-label">Current</div><div class="metric-val">{{ s.telemetry.get('current', 0) }} A</div></div></div>
                        <div class="col-6 col-sm-3"><div class="metric-box"><div class="metric-label">Power</div><div class="metric-val">{{ s.telemetry.get('activePower', 0) }} W</div></div></div>
                        <div class="col-6 col-sm-3"><div class="metric-box"><div class="metric-label">Freq</div><div class="metric-val">{{ s.telemetry.get('frequency', 0) }} Hz</div></div></div>
                    </div>

                    <div class="bg-light p-3 rounded-3 mt-3">
                        <div class="d-flex flex-wrap gap-2 mb-2">
                            <form action="/cmd" method="POST" class="d-inline flex-grow-1">
                                <input type="hidden" name="socket_id" value="{{ s.socket_id }}">
                                <div class="d-flex gap-2">
                                    <button type="submit" name="action" value="ON" class="btn btn-success flex-grow-1 interactive-el fw-bold" {% if has_fault %}disabled{% endif %}><i class="bi bi-toggle-on"></i> ON</button>
                                    <button type="submit" name="action" value="OFF" class="btn btn-outline-danger flex-grow-1 interactive-el fw-bold" {% if has_fault %}disabled{% endif %}><i class="bi bi-toggle-off"></i> OFF</button>
                                </div>
                            </form>
                        </div>
                        <div class="d-flex flex-wrap gap-2">
                            <button class="btn btn-outline-secondary flex-grow-1 interactive-el btn-sm" data-bs-toggle="collapse" data-bs-target="#cfg-{{ s.socket_id }}"><i class="bi bi-gear"></i> Limits</button>
                            <button class="btn btn-outline-primary flex-grow-1 interactive-el btn-sm" data-bs-toggle="collapse" data-bs-target="#sch-{{ s.socket_id }}"><i class="bi bi-calendar-range"></i> Advanced Schedule</button>
                        </div>
                    </div>

                    <div class="collapse mt-2" id="cfg-{{ s.socket_id }}">
                        <div class="card card-body border shadow-sm">
                            <form action="/config" method="POST">
                                <input type="hidden" name="socket_id" value="{{ s.socket_id }}">
                                <div class="row g-2">
                                    <div class="col-12 col-sm-4"><label class="small text-muted mb-1">Max Current (A)</label><input type="number" step="0.1" name="oc" class="form-control form-control-sm interactive-el" value="15.0" required></div>
                                    <div class="col-12 col-sm-4"><label class="small text-muted mb-1">Min Voltage (V)</label><input type="number" step="1" name="uv" class="form-control form-control-sm interactive-el" value="190" required></div>
                                    <div class="col-12 col-sm-4"><label class="small text-muted mb-1">Exp. Freq (Hz)</label><input type="number" name="freq" class="form-control form-control-sm interactive-el" value="50" required></div>
                                </div>
                                <button type="submit" class="btn btn-dark btn-sm mt-3 w-100 interactive-el">Update Limits</button>
                            </form>
                        </div>
                    </div>

                    <div class="collapse mt-2" id="sch-{{ s.socket_id }}">
                        <div class="card border border-primary border-opacity-25 shadow-sm">
                            <div class="card-header bg-transparent border-bottom-0 pb-0">
                                <ul class="nav nav-tabs card-header-tabs" role="tablist">
                                    <li class="nav-item"><button class="nav-link active interactive-el" data-bs-toggle="tab" data-bs-target="#oneoff-{{ s.socket_id }}">One-Time</button></li>
                                    <li class="nav-item"><button class="nav-link interactive-el" data-bs-toggle="tab" data-bs-target="#recurring-{{ s.socket_id }}">Recurring</button></li>
                                </ul>
                            </div>
                            <div class="card-body tab-content">
                                
                                <div class="tab-pane fade show active" id="oneoff-{{ s.socket_id }}" role="tabpanel">
                                    <form action="/schedule" method="POST">
                                        <input type="hidden" name="socket_id" value="{{ s.socket_id }}">
                                        <input type="hidden" name="sched_type" value="one_off">
                                        <p class="small text-muted mb-2">Set absolute dates. Spanning multiple days or months is supported natively by the ESP32.</p>
                                        <div class="row g-2 mb-3">
                                            <div class="col-6"><label class="small fw-bold">Turn ON:</label><input type="datetime-local" name="on_datetime" class="form-control form-control-sm interactive-el" required></div>
                                            <div class="col-6"><label class="small fw-bold">Turn OFF:</label><input type="datetime-local" name="off_datetime" class="form-control form-control-sm interactive-el" required></div>
                                        </div>
                                        <button type="submit" class="btn btn-primary btn-sm w-100 interactive-el fw-bold"><i class="bi bi-send"></i> Set Absolute Timer</button>
                                    </form>
                                </div>

                                <div class="tab-pane fade" id="recurring-{{ s.socket_id }}" role="tabpanel">
                                    <form action="/schedule" method="POST">
                                        <input type="hidden" name="socket_id" value="{{ s.socket_id }}">
                                        <input type="hidden" name="sched_type" value="recurring">
                                        <p class="small text-muted mb-2">Automate this socket on specific days. The server will constantly re-arm the ESP32.</p>
                                        <div class="row g-2 mb-2">
                                            <div class="col-6"><label class="small fw-bold">Time ON:</label><input type="time" name="time_on" class="form-control form-control-sm interactive-el" required></div>
                                            <div class="col-6"><label class="small fw-bold">Time OFF:</label><input type="time" name="time_off" class="form-control form-control-sm interactive-el" required></div>
                                        </div>
                                        <div class="mb-3 d-flex flex-wrap gap-1">
                                            {% for day_val, day_name in [(0,'Mon'), (1,'Tue'), (2,'Wed'), (3,'Thu'), (4,'Fri'), (5,'Sat'), (6,'Sun')] %}
                                            <div>
                                                <input type="checkbox" id="d{{ day_val }}-{{ s.socket_id }}" name="days" value="{{ day_val }}" class="day-checkbox interactive-el">
                                                <label for="d{{ day_val }}-{{ s.socket_id }}" class="day-label">{{ day_name }}</label>
                                            </div>
                                            {% endfor %}
                                        </div>
                                        <button type="submit" class="btn btn-primary btn-sm w-100 interactive-el fw-bold"><i class="bi bi-repeat"></i> Start Automation</button>
                                    </form>
                                </div>

                                <hr class="my-3">
                                <form action="/cmd" method="POST">
                                    <input type="hidden" name="socket_id" value="{{ s.socket_id }}">
                                    <input type="hidden" name="action" value="CANCEL_TIMER">
                                    <button type="submit" class="btn btn-outline-warning btn-sm w-100 interactive-el"><i class="bi bi-x-circle"></i> Clear Active Schedule Memory</button>
                                </form>

                            </div>
                        </div>
                    </div>

                </div>
            </div>
        </div>
        {% else %}
        <div class="col-12"><div class="alert alert-info py-4 text-center rounded-4"><i class="bi bi-hourglass-split fs-2 d-block mb-2"></i><strong>No sockets discovered.</strong> Waiting for MQTT...</div></div>
        {% endfor %}
    </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
<script>
    let countdown = 10;
    const statusBadge = document.getElementById('refresh-status');
    let isPaused = false;
    
    setInterval(() => {
        if (!isPaused) {
            countdown--;
            if (countdown <= 0) window.location.reload();
            else statusBadge.innerHTML = `<i class="bi bi-arrow-repeat"></i> Auto-refreshing (${countdown}s)`;
        }
    }, 1000);

    const interactiveElements = document.querySelectorAll('.interactive-el, input, select');
    function pauseRefresh() {
        if (!isPaused) {
            isPaused = true;
            statusBadge.innerHTML = '<i class="bi bi-pause-circle"></i> Auto-refresh paused';
            statusBadge.classList.replace('bg-primary', 'bg-warning');
            statusBadge.classList.replace('text-white', 'text-dark');
        }
    }
    interactiveElements.forEach(el => {
        el.addEventListener('focus', pauseRefresh);
        el.addEventListener('click', pauseRefresh);
    });
</script>
</body>
</html>
"""

# --- FLASK ROUTES ---
@app.route('/')
def index():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM sockets ORDER BY socket_id ASC")
    rows = c.fetchall()
    conn.close()

    sockets = []
    for row in rows:
        r_dict = dict(row)
        
        # Parse Telemetry
        try: 
            r_dict['telemetry'] = json.loads(r_dict['telemetry'])
        except json.JSONDecodeError: 
            r_dict['telemetry'] = {}
            
        # Parse Fault State published by ESP32
        try:
            r_dict['fault_state'] = json.loads(r_dict['fault_state'])
        except json.JSONDecodeError:
            r_dict['fault_state'] = {}
            
        sockets.append(r_dict)

    return render_template_string(HTML_TEMPLATE, sockets=sockets)

@app.route('/cmd', methods=['POST'])
def send_cmd():
    socket_id = request.form['socket_id']
    action = request.form['action']
    
    if action in ['ON', 'OFF']:
        mqtt_client.publish(f"cmd/{socket_id}/relay", action)
    elif action == 'RESET':
        # Issues the clear command to the ESP32 which unsets the local fault flag
        mqtt_client.publish(f"cmd/{socket_id}/reset", "1")
    elif action == 'CANCEL_TIMER':
        mqtt_client.publish(f"cmd/{socket_id}/timer", "OFF")
        
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("UPDATE schedules SET is_active=0 WHERE socket_id=?", (socket_id,))
        conn.commit()
        conn.close()
        
    return redirect(url_for('index'))

@app.route('/config', methods=['POST'])
def send_config():
    socket_id = request.form['socket_id']
    payload = f"{request.form['oc']},{request.form['uv']},{request.form['freq']}"
    mqtt_client.publish(f"cmd/{socket_id}/config", payload)
    return redirect(url_for('index'))

@app.route('/schedule', methods=['POST'])
def set_schedule():
    socket_id = request.form['socket_id']
    sched_type = request.form['sched_type']
    
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    c.execute("UPDATE schedules SET is_active=0 WHERE socket_id=?", (socket_id,))

    if sched_type == 'one_off':
        try:
            on_dt = datetime.strptime(request.form['on_datetime'], '%Y-%m-%dT%H:%M')
            off_dt = datetime.strptime(request.form['off_datetime'], '%Y-%m-%dT%H:%M')
            on_epoch = int(on_dt.timestamp())
            off_epoch = int(off_dt.timestamp())
            
            c.execute("INSERT INTO schedules (socket_id, sched_type, on_epoch, off_epoch) VALUES (?, ?, ?, ?)", 
                      (socket_id, sched_type, on_epoch, off_epoch))
            
            mqtt_client.publish(f"cmd/{socket_id}/timer", f"{on_epoch},{off_epoch}")
        except ValueError:
            return "Invalid date format", 400

    elif sched_type == 'recurring':
        time_on = request.form['time_on']
        time_off = request.form['time_off']
        
        days_selected = request.form.getlist('days') 
        days_str = ",".join(days_selected) if days_selected else "0,1,2,3,4,5,6"
        
        c.execute("INSERT INTO schedules (socket_id, sched_type, time_on, time_off, days) VALUES (?, ?, ?, ?, ?)", 
                  (socket_id, sched_type, time_on, time_off, days_str))

    conn.commit()
    conn.close()
    
    return redirect(url_for('index'))

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=5000, debug=False)
