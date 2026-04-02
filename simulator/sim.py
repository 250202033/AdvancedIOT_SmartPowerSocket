import os
import time
import json
import sqlite3
import random
import threading
from datetime import datetime, timedelta
import paho.mqtt.client as mqtt
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

# --- GLOBALS & LOCKS ---
db_lock = threading.Lock()
SHOW_TELEMETRY_LOGS = True  # Toggle via CLI

# --- APPLIANCE PROFILES ---
PROFILES = {
    "kettle":    {"voltage": 220.0, "power": 2000.0, "pf": 1.00, "type": "timer"},
    "microwave": {"voltage": 220.0, "power": 800.0,  "pf": 0.98, "type": "timer"},
    "laptop":    {"voltage": 220.0, "power": 65.0,   "pf": 0.85, "type": "continuous"},
    "lamp":      {"voltage": 220.0, "power": 12.0,   "pf": 0.95, "type": "continuous"},
    "tv":        {"voltage": 220.0, "power": 120.0,  "pf": 0.90, "type": "continuous"},
    "fan":       {"voltage": 220.0, "power": 55.0,   "pf": 0.80, "type": "continuous"},
    "fridge":    {"voltage": 220.0, "power": 150.0,  "pf": 0.85, "type": "continuous"},
    "ac":        {"voltage": 220.0, "power": 1500.0, "pf": 0.95, "type": "continuous"},
    "motor":     {"voltage": 220.0, "power": 2400.0, "pf": 0.45, "type": "continuous"}
}

# --- CONFIGURATION ---
CONFIG = {
    "broker_ip": "127.0.0.1",
    "broker_port": 8883,
    "db_file": "sim_devices.sqlite",
    "certs_dir": "./certs",
    "root_ca_cert": "ca.crt", 
    "root_ca_key": "ca.key",
    "num_devices_to_create": 10,
    "appliance_types": list(PROFILES.keys())
}

# --- mTLS CERTIFICATE GENERATOR ---
def generate_client_certs(hostname):
    os.makedirs(CONFIG["certs_dir"], exist_ok=True)
    client_cert_path = f"{CONFIG['certs_dir']}/{hostname}.crt"
    client_key_path = f"{CONFIG['certs_dir']}/{hostname}.key"

    if os.path.exists(client_cert_path) and os.path.exists(client_key_path):
        return client_cert_path, client_key_path
    
    try:
        with open(CONFIG["root_ca_cert"], "rb") as f:
            ca_cert = x509.load_pem_x509_certificate(f.read())
        with open(CONFIG["root_ca_key"], "rb") as f:
            ca_key = serialization.load_pem_private_key(f.read(), password=None)
    except FileNotFoundError:
        print(f"\n[!] ERROR: Root CA not found. Need {CONFIG['root_ca_cert']} and {CONFIG['root_ca_key']}")
        exit(1)

    client_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    
    client_cert = x509.CertificateBuilder().subject_name(
        subject
    ).issuer_name(
        ca_cert.subject
    ).public_key(
        client_key.public_key()
    ).serial_number(
        x509.random_serial_number()
    ).not_valid_before(
        datetime.utcnow()
    ).not_valid_after(
        datetime.utcnow() + timedelta(days=365)
    ).sign(ca_key, hashes.SHA256())

    with open(client_key_path, "wb") as f:
        f.write(client_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption()
        ))
    with open(client_cert_path, "wb") as f:
        f.write(client_cert.public_bytes(serialization.Encoding.PEM))

    return client_cert_path, client_key_path

# --- DATABASE SETUP ---
def init_db():
    conn = sqlite3.connect(CONFIG["db_file"], check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS devices (
            hostname TEXT PRIMARY KEY,
            appliance TEXT,
            relay_state INTEGER,
            energy REAL
        )
    ''')
    conn.commit()
    return conn

def setup_devices(conn):
    cursor = conn.cursor()
    cursor.execute("SELECT hostname FROM devices")
    if len(cursor.fetchall()) == 0:
        print(f"First start: Generating {CONFIG['num_devices_to_create']} simulated devices...")
        for _ in range(CONFIG["num_devices_to_create"]):
            mac_suffix = ''.join(random.choices("0123456789ABCDEF", k=6))
            hostname = f"Socket-{mac_suffix}"
            appliance = random.choice(CONFIG["appliance_types"])
            cursor.execute("INSERT INTO devices (hostname, appliance, relay_state, energy) VALUES (?, ?, ?, ?)",
                           (hostname, appliance, 0, 0.0))
        conn.commit()

# --- DEVICE SIMULATOR CLASS ---
class SimulatedSocket:
    def __init__(self, conn, hostname, appliance, initial_relay, initial_energy):
        self.conn = conn
        self.hostname = hostname
        self.appliance = appliance
        self.energy = initial_energy
        self.profile = PROFILES[appliance]
        self.running = True
        
        # State Variables
        self.relay_state = bool(initial_relay)
        self.is_fault_detected = False
        self.current_fault = "NONE"
        
        # Timer Variables
        self.timer_active = False
        self.timer_on_time = 0
        self.timer_off_time = 0
        
        # Config Limits
        self.limit_over_current = 15.0
        self.limit_under_voltage = 190.0
        self.limit_expected_freq = 50

        # Current Telemetry (for fault checking)
        self.current_voltage = 0.0
        self.current_amps = 0.0
        self.current_freq = 0.0
        
        # Topics
        self.topic_telemetry = f"telemetry/{hostname}/pzem"
        self.topic_state_relay = f"state/{hostname}/relay"
        self.topic_state_fault = f"state/{hostname}/fault"
        self.topic_state_timer = f"state/{hostname}/timer"
        self.topic_cmd = f"cmd/{hostname}/#"
        
        # Heating Cycle State
        self.active_heating = False
        self.active_timer = 0
        
        # Anomaly Injection (CLI testing)
        self.inject_anomaly = None
        
        client_cert, client_key = generate_client_certs(hostname)
        self.client = mqtt.Client(client_id=hostname, protocol=mqtt.MQTTv311)
        self.client.tls_set(ca_certs=CONFIG["root_ca_cert"], certfile=client_cert, keyfile=client_key)
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        
    def start(self):
        try:
            self.client.connect(CONFIG["broker_ip"], CONFIG["broker_port"], 60)
            self.client.loop_start()
            threading.Thread(target=self.telemetry_loop, daemon=True).start()
            threading.Thread(target=self.state_telemetry_loop, daemon=True).start()
        except Exception as e:
            print(f"[{self.hostname}] Connection failed: {e}")

    def on_connect(self, client, userdata, flags, rc):
        print(f"[{self.hostname}] Connected to MQTT Broker.")
        self.client.subscribe(self.topic_cmd)
        self.publish_relay_state()
        self.publish_fault_state()
        self.publish_timer_state()

    def set_relay_state(self, state, source):
        if self.is_fault_detected and state is True:
            print(f"[{self.hostname}] Cannot turn ON relay: Fault active. Reset fault first.")
            return

        if self.relay_state != state:
            self.relay_state = state
            print(f"[{self.hostname}] >> Relay turned {'ON' if state else 'OFF'} via {source}")
            self.publish_relay_state()
            self.update_db_state()

            # Timer is invalidated on manual override
            if self.timer_active and source != "Timer":
                self.timer_active = False
                print(f"[{self.hostname}] >> Timer invalidated due to manual override.")
                self.publish_timer_state()

    def on_message(self, client, userdata, msg):
        payload = msg.payload.decode().strip()
        print(f"[{self.hostname}] Rx MQTT [{msg.topic}]: {payload}")
        subtopic = msg.topic.split('/')[-1]
        
        if subtopic == "relay":
            if payload.upper() in ["ON", "1"]:
                self.set_relay_state(True, "MQTT")
                if self.profile["type"] == "timer":
                    self.active_heating = True
                    self.active_timer = time.time() + random.randint(60, 180)
            elif payload.upper() in ["OFF", "0"]:
                self.set_relay_state(False, "MQTT")
                self.active_heating = False
                
        elif subtopic == "reset":
            if payload.lower() in ["1", "true"]:
                print(f"[{self.hostname}] >> System: Fault flags and state reset cleared.")
                self.set_relay_state(False, "ServerReset")
                self.energy = 0.0
                self.is_fault_detected = False
                self.current_fault = "NONE"
                self.timer_active = False
                self.update_db_state()
                self.publish_fault_state()
                self.publish_timer_state()
                
        elif subtopic == "timer":
            if payload.upper() == "OFF":
                self.timer_active = False
                print(f"[{self.hostname}] >> Timer disabled via MQTT.")
                self.publish_timer_state()
            else:
                try:
                    on_t, off_t = map(int, payload.split(','))
                    now = int(time.time())
                    if on_t > now and off_t > on_t:
                        self.timer_on_time = on_t
                        self.timer_off_time = off_t
                        self.timer_active = True
                        print(f"[{self.hostname}] >> Timer updated via MQTT (On: {on_t}, Off: {off_t}).")
                        self.publish_timer_state()
                    else:
                        print(f"[{self.hostname}] >> Invalid timer timestamps received.")
                except ValueError:
                    print(f"[{self.hostname}] >> Failed to parse timer payload.")
                    
        elif subtopic == "config":
            try:
                oc, uv, freq = map(float, payload.split(','))
                self.limit_over_current = oc
                self.limit_under_voltage = uv
                self.limit_expected_freq = int(freq)
                print(f"[{self.hostname}] >> Config updated: OC={oc}A, UV={uv}V, Freq={freq}Hz")
            except ValueError:
                pass

    def check_software_faults(self):
        if self.is_fault_detected: return
        
        fault_tripped = False
        if self.current_amps > self.limit_over_current:
            self.current_fault = "SW_OVERCURRENT"
            fault_tripped = True
        elif 0 < self.current_voltage < self.limit_under_voltage:
            self.current_fault = "UNDER_VOLTAGE"
            fault_tripped = True
        elif self.current_freq > 0 and abs(self.current_freq - self.limit_expected_freq) > 2.0:
            self.current_fault = "FREQ_ERROR"
            fault_tripped = True

        if fault_tripped:
            self.is_fault_detected = True
            print(f"\n[!] [{self.hostname}] >> Software fault detected ({self.current_fault})! Relay tripped.")
            self.set_relay_state(False, "Software Protection")
            self.publish_fault_state()

    def handle_timer_logic(self):
        if not self.timer_active: return
        now = int(time.time())
        if self.timer_on_time <= now < self.timer_off_time:
            if not self.relay_state:
                self.set_relay_state(True, "Timer")
        elif now >= self.timer_off_time:
            if self.relay_state:
                self.set_relay_state(False, "Timer")
            self.timer_active = False
            self.publish_timer_state()

    # --- PUBLISHERS ---
    def update_db_state(self):
        with db_lock:
            cursor = self.conn.cursor()
            cursor.execute("UPDATE devices SET relay_state = ?, energy = ?, appliance = ? WHERE hostname = ?",
                           (int(self.relay_state), self.energy, self.appliance, self.hostname))
            self.conn.commit()

    def publish_relay_state(self):
        payload = json.dumps({"state": "ON" if self.relay_state else "OFF"})
        self.client.publish(self.topic_state_relay, payload, retain=True)
        
    def publish_fault_state(self):
        payload = json.dumps({
            "fault": self.is_fault_detected,
            "reason": self.current_fault
        })
        self.client.publish(self.topic_state_fault, payload, retain=True)
        
    def publish_timer_state(self):
        payload = json.dumps({
            "active": self.timer_active,
            "onTime": self.timer_on_time,
            "offTime": self.timer_off_time
        })
        self.client.publish(self.topic_state_timer, payload, retain=True)

    def state_telemetry_loop(self):
        while self.running:
            time.sleep(1.0)
            self.publish_relay_state()
            self.publish_fault_state()
            self.publish_timer_state()

    # --- CORE LOOP ---
    def telemetry_loop(self):
        while self.running:
            time.sleep(2.0)
            self.handle_timer_logic()
            
            # Base generation
            if not self.relay_state:
                voltage = self.profile["voltage"] + random.uniform(-2, 2)
                current, activeP, apparentP, reactiveP, pf = 0.0, 0.0, 0.0, 0.0, 0.0
            else:
                voltage = self.profile["voltage"] + random.uniform(-3, 3)
                
                if self.profile["type"] == "timer":
                    if self.active_heating:
                        activeP = self.profile["power"] + random.uniform(-50, 50)
                        if time.time() > self.active_timer:
                            self.active_heating = False
                    else:
                        activeP = 0.0
                else:
                    activeP = self.profile["power"] + random.uniform(-self.profile["power"]*0.05, self.profile["power"]*0.05)
                
                pf = self.profile["pf"] + random.uniform(-0.02, 0.02)
                if pf > 1.0: pf = 1.0
                
                apparentP = activeP / pf if activeP > 0 else 0.0
                current = apparentP / voltage if voltage > 0 else 0.0
                reactiveP = (apparentP**2 - activeP**2)**0.5 if apparentP > activeP else 0.0

            frequency = 50.0 + random.uniform(-0.1, 0.1)
            
            # Apply Fault Injections (from CLI)
            if self.inject_anomaly == "voltage":
                voltage = random.uniform(100.0, 150.0) # Under voltage
                self.inject_anomaly = None
            elif self.inject_anomaly == "current":
                current = random.uniform(20.0, 30.0) # Over current
                self.inject_anomaly = None
            elif self.inject_anomaly == "frequency":
                frequency = random.uniform(40.0, 45.0) # Freq Error
                self.inject_anomaly = None

            # Update state variables
            self.current_voltage = voltage
            self.current_amps = current
            self.current_freq = frequency

            if activeP > 0:
                self.energy += (activeP * (2.0 / 3600.0))
                self.update_db_state()

            payload = json.dumps({
                "voltage": round(voltage, 2),
                "current": round(current, 2),
                "activePower": round(activeP, 2),
                "apparentPower": round(apparentP, 2),
                "reactivePower": round(reactiveP, 2),
                "powerFactor": round(pf, 2),
                "frequency": round(frequency, 1),
                "energy": int(self.energy)
            })
            
            self.client.publish(self.topic_telemetry, payload)
            self.check_software_faults()
            
            if SHOW_TELEMETRY_LOGS:
                print(f"[{self.hostname}] Telemetry: {round(voltage,1)}V | {round(current,2)}A | {round(activeP,1)}W")

# --- CLI HELPERS ---
def print_menu():
    print("\n" + "="*45)
    print(" MQTT ESP32 PZEM SIMULATOR - CONTROL PANEL")
    print("="*45)
    print(" 1. List Active Devices")
    print(" 2. Add New Device")
    print(" 3. Change Device Appliance Profile")
    print(" 4. Trigger Fault Anomaly on Device")
    print(f" 5. Toggle Telemetry Logs (Currently: {'ON' if SHOW_TELEMETRY_LOGS else 'OFF'})")
    print(" 6. Exit")
    print("="*45)

# --- MAIN RUNNER ---
def main():
    global SHOW_TELEMETRY_LOGS
    print("--- Starting PZEM Smart Socket Simulator ---")
    conn = init_db()
    setup_devices(conn)
    
    cursor = conn.cursor()
    cursor.execute("SELECT hostname, appliance, relay_state, energy FROM devices")
    simulators = []
    
    for row in cursor.fetchall():
        hostname, appliance, relay_state, energy = row
        sim = SimulatedSocket(conn, hostname, appliance, relay_state, energy)
        simulators.append(sim)
        sim.start()

    time.sleep(1) # Let initial logs print

    try:
        while True:
            print_menu()
            choice = input("Select an option (1-6): ").strip()
            
            if choice == '1':
                print("\n--- Active Devices ---")
                for i, sim in enumerate(simulators, 1):
                    state = "ON " if sim.relay_state else "OFF"
                    fault = sim.current_fault if sim.is_fault_detected else "OK"
                    print(f" {i:02d}. {sim.hostname} | {sim.appliance.ljust(10)} | Rly: {state} | Fault: {fault.ljust(15)} | Engy: {int(sim.energy)}Wh")
            
            elif choice == '2':
                print("\n--- Add New Device ---")
                app_list = list(PROFILES.keys())
                for i, app in enumerate(app_list, 1):
                    print(f" {i}. {app}")
                try:
                    app_idx = int(input(f"Select appliance type (1-{len(app_list)}): ")) - 1
                    if 0 <= app_idx < len(app_list):
                        appliance = app_list[app_idx]
                        mac_suffix = ''.join(random.choices("0123456789ABCDEF", k=6))
                        hostname = f"Socket-{mac_suffix}"
                        with db_lock:
                            cursor = conn.cursor()
                            cursor.execute("INSERT INTO devices (hostname, appliance, relay_state, energy) VALUES (?, ?, ?, ?)",
                                           (hostname, appliance, 0, 0.0))
                            conn.commit()
                        sim = SimulatedSocket(conn, hostname, appliance, 0, 0.0)
                        simulators.append(sim)
                        sim.start()
                        print(f"\n[+] Added {hostname} ({appliance})")
                except ValueError: pass

            elif choice == '3':
                for i, sim in enumerate(simulators, 1):
                    print(f" {i:02d}. {sim.hostname} (Current: {sim.appliance})")
                try:
                    dev_idx = int(input("\nSelect device to modify: ")) - 1
                    if 0 <= dev_idx < len(simulators):
                        target_sim = simulators[dev_idx]
                        app_list = list(PROFILES.keys())
                        for i, app in enumerate(app_list, 1):
                            print(f" {i}. {app}")
                        app_idx = int(input("Select NEW appliance type: ")) - 1
                        if 0 <= app_idx < len(app_list):
                            new_app = app_list[app_idx]
                            target_sim.appliance = new_app
                            target_sim.profile = PROFILES[new_app]
                            target_sim.update_db_state()
                            print(f"\n[*] Updated {target_sim.hostname} to {new_app}")
                except ValueError: pass

            elif choice == '4':
                print("\n--- Trigger Fault ---")
                for i, sim in enumerate(simulators, 1):
                    print(f" {i:02d}. {sim.hostname}")
                try:
                    dev_idx = int(input("Select device: ")) - 1
                    if 0 <= dev_idx < len(simulators):
                        print(" 1. Under Voltage Dip\n 2. Over Current Spike\n 3. Frequency Drop")
                        f_idx = input("Select fault: ")
                        if f_idx == '1': simulators[dev_idx].inject_anomaly = "voltage"
                        elif f_idx == '2': simulators[dev_idx].inject_anomaly = "current"
                        elif f_idx == '3': simulators[dev_idx].inject_anomaly = "frequency"
                        print(f"[*] Anomaly queued for {simulators[dev_idx].hostname}. Watch logs.")
                except ValueError: pass

            elif choice == '5':
                SHOW_TELEMETRY_LOGS = not SHOW_TELEMETRY_LOGS
                print(f"\n[*] Telemetry Logs are now {'ON' if SHOW_TELEMETRY_LOGS else 'OFF'}.")

            elif choice == '6':
                print("\nShutting down simulators...")
                break
                
    except KeyboardInterrupt:
        print("\nForce quitting...")
        
    finally:
        for sim in simulators:
            sim.running = False
            sim.client.loop_stop()
            sim.client.disconnect()
        conn.close()
        print("Done.")

if __name__ == "__main__":
    main()
