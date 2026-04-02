import paho.mqtt.client as mqtt
import requests
import json
from datetime import datetime

# --- Configuration ---
MQTT_BROKER = "127.0.01"  # Replace with your MQTT broker IP
MQTT_PORT = 1883
MQTT_TOPIC = "state/+/fault"   

TELEGRAM_BOT_TOKEN = ""
TELEGRAM_CHAT_ID = "" # Your family group chat ID

# --- State Tracker ---
# This dictionary remembers the current state of each socket.
# Example: {"Socket-AA1122": True, "Socket-BB3344": False}
active_faults = {}

def send_telegram_alert(message):
    """Sends a push notification to the specified Telegram chat."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        response = requests.post(url, data=payload, timeout=10)
        if response.status_code != 200:
            print(f"Failed to send Telegram message: {response.text}")
    except Exception as e:
        print(f"Telegram API Error: {e}")

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("✅ Connected to MQTT broker successfully.")
        client.subscribe(MQTT_TOPIC)
        print(f"📡 Listening for faults on topic: {MQTT_TOPIC}")
    else:
        print(f"❌ Failed to connect, return code {rc}")

def on_message(client, userdata, msg):
    try:
        # Extract socket hostname
        topic_parts = msg.topic.split('/')
        hostname = topic_parts[1] if len(topic_parts) >= 2 else "Unknown_Socket"

        # Parse the JSON payload
        payload = json.loads(msg.payload.decode('utf-8'))
        
        # Check current fault state reported by ESP32
        is_fault_reported = payload.get("fault")
        is_fault_bool = (is_fault_reported is True or str(is_fault_reported).lower() == "true")
        reason = payload.get("reason", "NONE")
        
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # Look up the LAST known state of this specific socket (defaults to False if unseen)
        was_already_faulty = active_faults.get(hostname, False)

        # Logic 1: NEW FAULT DETECTED
        if is_fault_bool and not was_already_faulty:
            active_faults[hostname] = True  # Update memory to block spam
            
            alert_message = (
                f"🚨 *Smart Socket Fault Detected* 🚨\n\n"
                f"🔌 *Device:* `{hostname}`\n"
                f"⚠️ *Reason:* `{reason}`\n"
                f"🕒 *Time:* {current_time}"
            )
            print(f"[{current_time}] 🔴 NEW FAULT on {hostname}. Alerting Telegram...")
            send_telegram_alert(alert_message)

        # Logic 2: FAULT RESOLVED (It was faulty, but now reports false)
        elif not is_fault_bool and was_already_faulty:
            active_faults[hostname] = False # Reset memory so it can trigger again later
            
            resolve_message = (
                f"✅ *Smart Socket Restored* ✅\n\n"
                f"🔌 *Device:* `{hostname}`\n"
                f"ℹ️ *Status:* Fault Cleared / Back to Normal\n"
                f"🕒 *Time:* {current_time}"
            )
            print(f"[{current_time}] 🟢 FAULT RESOLVED on {hostname}. Alerting Telegram...")
            send_telegram_alert(resolve_message)

        # Logic 3: Ignored (Spam prevention)
        # If it's still faulty (True -> True) or still normal (False -> False), it does nothing silently.

    except json.JSONDecodeError:
        print(f"⚠️ Error decoding JSON from topic {msg.topic}: {msg.payload}")
    except Exception as e:
        print(f"⚠️ Error processing message: {e}")

if __name__ == "__main__":
    client = mqtt.Client()
    client.on_connect = on_connect
    client.on_message = on_message

    try:
        print(f"Connecting to MQTT broker at {MQTT_BROKER}...")
        client.connect(MQTT_BROKER, MQTT_PORT, 60)
        client.loop_forever()
    except KeyboardInterrupt:
        print("\nExiting script.")
    except Exception as e:
        print(f"❌ Could not connect to MQTT Broker: {e}")
