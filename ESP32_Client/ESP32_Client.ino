#include "soc/gpio_reg.h"
#include <SCT013.h>
#include <MycilaPZEM.h>
#include <WiFi.h>
#include <time.h>
#include <esp_sntp.h>
#include <WiFiClientSecure.h>
#include <PubSubClient.h>
#include <strings.h>
#include <stdio.h>
#include <pgmspace.h>

// --- PIN DEFINITIONS ---
const int RX2_PIN = 16;
const int TX2_PIN = 17;
const uint8_t RELAY_PIN = 26; 
const int ADC_PIN = 34;

// --- SENSOR OBJECTS ---
Mycila::PZEM pzem;
SCT013 sensor(ADC_PIN);

// --- GLOBAL VARIABLES FOR PZEM DATA ---
float pzemFrequency = 0.0;
float pzemVoltage = 0.0;
float pzemCurrent = 0.0;
float pzemActivePower = 0.0;
float pzemPowerFactor = 0.0;
float pzemApparentPower = 0.0;
float pzemReactivePower = 0.0;
uint32_t pzemActiveEnergy = 0;
bool pzemValid = false;

// --- FAULT DETECTION CONSTANTS & CONFIGURATIONS ---
const int DC_BIAS_CENTER = 2048;
const int FAULT_AMPLITUDE_THRESHOLD = 900;
const unsigned long INRUSH_SAFE_DURATION_MS = 200;

// Configurable Limits (Updated via MQTT)
float limitOverCurrent = 15.0;     // Amps
float limitUnderVoltage = 190.0;   // Volts
int   limitExpectedFreq = 50;      // Hertz (50 or 60)

// --- STATE VARIABLES ---
unsigned long faultStartTime = 0;
bool isFaultDetected = false;
bool relayState = false;

enum FaultReason 
{
    FAULT_NONE,
    FAULT_HW_OVERCURRENT,
    FAULT_SW_OVERCURRENT,
    FAULT_UNDER_VOLTAGE,
    FAULT_FREQ_ERROR
};
FaultReason currentFault = FAULT_NONE;

// --- TIMER VARIABLES ---
bool timerActive = false;
time_t timerOnTime = 0;  // Epoch time 'b'
time_t timerOffTime = 0; // Epoch time 'c'

// Timing Variables
unsigned long lastStateTelemetryTime = 0;
unsigned long lastTelemetryTime = 0;
unsigned long lastHeapReportTime = 0;

// Intervals
const unsigned long STATE_TELEMETRY_INTERVAL = 1000;
const unsigned long TELEMETRY_INTERVAL = 2000; // Publish every 2 seconds
const unsigned long HEAP_REPORT_INTERVAL = 60000; // Log memory every minute

// NTP Settings
const char* ntpServer1 = "pool.ntp.org";
const char* ntpServer2 = "time.google.com";
const char* ntpServer3 = "time.cloudflare.com";
const char* time_zone = "ICT-7";

// Server Configs
char serverIP[16] = "0.0.0.0";
const char* serverHostName = "raspberrypi";

// Root CA and client cert and private key (PROGMEM saves SRAM)
const char ca_cert[] PROGMEM = R"EOF(-----BEGIN CERTIFICATE-----
-----END CERTIFICATE-----
)EOF";
const char client_cert[] PROGMEM = R"EOF(-----BEGIN CERTIFICATE-----
-----END CERTIFICATE-----
)EOF";
const char client_key[] PROGMEM = R"EOF(-----BEGIN RSA PRIVATE KEY-----
-----END RSA PRIVATE KEY-----
)EOF";

// MQTT setup
WiFiClientSecure secureClient;
PubSubClient mqttClient(secureClient);

// --- WIFI CONFIG ---
const char* ssid = "IOT24VN";
const char* password = "Caohoc2017";
char hostname[20];

// Dynamic Topic Buffers
char topicTelemetry[64];
char topicStateRelay[64];
char topicStateFault[64];
char topicStateTimer[64];
char topicCmd[64];

// --- FORWARD DECLARATIONS ---
void fastDigitalWrite(uint8_t pin, uint8_t value);
void forceTimeSync();
void checkLocalTime();
void resolveHostToBuffer(const char* hostname, char* buffer, size_t len);
void maintainConnections();
void processSerialCommands();
void publishTelemetry();
void publishRelayState();
void publishFaultState();
void publishTimerState();
void setRelayState(bool state, const char* source);
void mqttCallback(char* topic, byte* payload, unsigned int length);
void checkSoftwareFaults();
void handleTimerLogic();

// --- FAST GPIO WRAPPER ---
void IRAM_ATTR fastDigitalWrite(uint8_t pin, uint8_t value)
{
    if (value) 
    {
        REG_WRITE(GPIO_OUT_W1TS_REG, 1UL << pin);
    }
    else 
    {
        REG_WRITE(GPIO_OUT_W1TC_REG, 1UL << pin);
    }
}

void setup()
{
    Serial.begin(115200);
       
    pinMode(RELAY_PIN, OUTPUT);
    fastDigitalWrite(RELAY_PIN, 0); 
    relayState = false;

    analogReadResolution(12);
    sensor.begin(2000, 75); 
    sensor.setFrequency(50); 

    pzem.begin(Serial2, RX2_PIN, TX2_PIN, 0x01, true);
    pzem.setCallback([](const Mycila::PZEM::EventType evt, const Mycila::PZEM::Data& data)
    {
        if (evt == Mycila::PZEM::EventType::EVT_READ)
        {
            pzemFrequency = data.frequency;
            pzemVoltage = data.voltage;
            pzemCurrent = data.current;
            pzemActivePower = data.activePower;
            pzemPowerFactor = data.powerFactor;
            pzemApparentPower = data.apparentPower;
            pzemReactivePower = data.reactivePower;
            pzemActiveEnergy = data.activeEnergy;
            pzemValid = true;
        }
    });

    WiFi.mode(WIFI_STA); 
    delay(100); 

    uint8_t mac[6];
    WiFi.macAddress(mac);
    snprintf(hostname, sizeof(hostname), "Socket-%02X%02X%02X", mac[3], mac[4], mac[5]);
    
    snprintf(topicTelemetry, sizeof(topicTelemetry), "telemetry/%s/pzem", hostname);
    snprintf(topicStateRelay, sizeof(topicStateRelay), "state/%s/relay", hostname);
    snprintf(topicStateFault, sizeof(topicStateFault), "state/%s/fault", hostname);
    snprintf(topicStateTimer, sizeof(topicStateTimer), "state/%s/timer", hostname);
    snprintf(topicCmd, sizeof(topicCmd), "cmd/%s/#", hostname);

    Serial.printf("Setting hostname to: %s\n", hostname);
    WiFi.setHostname(hostname);
    WiFi.setAutoReconnect(true);
    WiFi.persistent(true);
    WiFi.begin(ssid, password);
    Serial.print("Connecting to WiFi");
    
    unsigned long startAttempt = millis();
    while (WiFi.status() != WL_CONNECTED && (millis() - startAttempt < 20000))
    {
        delay(500);
        Serial.print(".");
    }
    
    if (WiFi.status() == WL_CONNECTED) {
        Serial.println("\nWiFi connected!");
    } else {
        Serial.println("\nWiFi connection failed! Rebooting...");
        ESP.restart();
    }

    forceTimeSync();
    checkLocalTime();

    resolveHostToBuffer(serverHostName, serverIP, sizeof(serverIP));
    
    secureClient.setCACert(ca_cert);
    secureClient.setCertificate(client_cert);
    secureClient.setPrivateKey(client_key);
    
    // Set explicit timeout for SSL handshakes to prevent blocking
    secureClient.setTimeout(5000); 
    
    mqttClient.setServer(serverIP, 8883); 
    mqttClient.setCallback(mqttCallback);
    // Increase MQTT buffer size if dealing with large custom payloads
    mqttClient.setBufferSize(512); 
}

void loop()
{
    // 1. HIGH-SPEED ADC POLLING (Fault Tripping)
    int rawAdc = analogRead(ADC_PIN);
    int amplitude = abs(rawAdc - DC_BIAS_CENTER);

    if (amplitude > FAULT_AMPLITUDE_THRESHOLD) 
    {
        if (faultStartTime == 0) 
        {
            faultStartTime = millis(); 
        } 
        else if ((millis() - faultStartTime) > INRUSH_SAFE_DURATION_MS) 
        {
            if (!isFaultDetected) 
            {
                relayState = false; 
                fastDigitalWrite(RELAY_PIN, 0); 
                isFaultDetected = true;
                currentFault = FAULT_HW_OVERCURRENT;
                Serial.println("CRITICAL: Sustained overcurrent detected. Relay tripped OFF.");
                publishFaultState();
                publishRelayState();
            }
        }
    } 
    else 
    {
        faultStartTime = 0; 
    }

    // 2. Network & Command Processing
    processSerialCommands();
    maintainConnections();
    
    if (mqttClient.connected()) {
        mqttClient.loop();
    }

    // 3. Telemetry Publishing
    unsigned long currentMillis = millis();
    if (currentMillis - lastTelemetryTime > TELEMETRY_INTERVAL)
    {
        lastTelemetryTime = currentMillis;
        if (pzemValid)
        {
            publishTelemetry();
            checkSoftwareFaults();
        }
    }

    // 4. State Telemetry Publishing
    if (currentMillis - lastStateTelemetryTime > STATE_TELEMETRY_INTERVAL)
    {
        lastStateTelemetryTime = currentMillis;
        publishRelayState();
        publishFaultState();
        publishTimerState();
    }
    
    // 5. Heap Diagnostics 
    if (currentMillis - lastHeapReportTime > HEAP_REPORT_INTERVAL) {
        lastHeapReportTime = currentMillis;
        Serial.printf("System Health - Free Heap: %d bytes, Max Block: %d bytes\n", 
                      ESP.getFreeHeap(), ESP.getMaxAllocHeap());
    }

    // 6. Timer Logic
    handleTimerLogic();
}

void setRelayState(bool state, const char* source)
{
    if (isFaultDetected && state == true)
    {
        Serial.println("Cannot turn ON relay: Fault active. Please reset fault first.");
        return;
    }

    if (relayState != state)
    {
        relayState = state;
        fastDigitalWrite(RELAY_PIN, relayState);
        Serial.printf(">> Relay turned %s via %s\n", state ? "ON" : "OFF", source);
        publishRelayState();

        if (timerActive && (strcmp(source, "Timer") != 0))
        {
            timerActive = false;
            Serial.println(">> Timer invalidated due to manual override.");
            publishTimerState();
        }
    }
}

void handleTimerLogic()
{
    if (!timerActive) return;

    time_t now;
    time(&now);

    if (now >= timerOnTime && now < timerOffTime)
    {
        if (!relayState) setRelayState(true, "Timer");
    }
    else if (now >= timerOffTime)
    {
        if (relayState) setRelayState(false, "Timer");
        timerActive = false;
        publishTimerState();
    }
}

void checkSoftwareFaults()
{
    if (isFaultDetected) return; 

    bool faultTripped = false;

    if (pzemCurrent > limitOverCurrent)
    {
        currentFault = FAULT_SW_OVERCURRENT;
        faultTripped = true;
    }
    else if (pzemVoltage > 0 && pzemVoltage < limitUnderVoltage)
    {
        currentFault = FAULT_UNDER_VOLTAGE;
        faultTripped = true;
    }
    else if (pzemFrequency > 0 && abs(pzemFrequency - limitExpectedFreq) > 2.0) 
    {
        currentFault = FAULT_FREQ_ERROR;
        faultTripped = true;
    }

    if (faultTripped)
    {
        isFaultDetected = true;
        setRelayState(false, "Software Protection");
        publishFaultState();
        Serial.println(">> Software fault detected! Relay tripped.");
    }
}

void publishTelemetry()
{
    if (!mqttClient.connected()) return;

    char payload[256];
    float safeReactivePower = isnan(pzemReactivePower) ? 0.0 : pzemReactivePower;

    snprintf(payload, sizeof(payload), 
        "{\"voltage\":%.2f,\"current\":%.2f,\"activePower\":%.2f,\"apparentPower\":%.2f,\"reactivePower\":%.2f,\"powerFactor\":%.2f,\"frequency\":%.1f,\"energy\":%u}",
        pzemVoltage, pzemCurrent, pzemActivePower, pzemApparentPower, safeReactivePower, pzemPowerFactor, pzemFrequency, pzemActiveEnergy);
    
    mqttClient.publish(topicTelemetry, payload);
}

void publishRelayState()
{
    if (!mqttClient.connected()) return;
    char payload[32];
    snprintf(payload, sizeof(payload), "{\"state\":\"%s\"}", relayState ? "ON" : "OFF");
    mqttClient.publish(topicStateRelay, payload, true); 
}

void publishFaultState()
{
    if (!mqttClient.connected()) return;
    char payload[128];
    const char* reasonStr = "NONE";
    switch(currentFault)
    {
        case FAULT_HW_OVERCURRENT: reasonStr = "HW_OVERCURRENT"; break;
        case FAULT_SW_OVERCURRENT: reasonStr = "SW_OVERCURRENT"; break;
        case FAULT_UNDER_VOLTAGE: reasonStr = "UNDER_VOLTAGE"; break;
        case FAULT_FREQ_ERROR: reasonStr = "FREQ_ERROR"; break;
        default: break;
    }

    snprintf(payload, sizeof(payload), "{\"fault\":%s,\"reason\":\"%s\"}", 
             isFaultDetected ? "true" : "false", reasonStr);
    mqttClient.publish(topicStateFault, payload, true);
}

void publishTimerState()
{
    if (!mqttClient.connected()) return;
    char payload[128];
    snprintf(payload, sizeof(payload), "{\"active\":%s,\"onTime\":%ld,\"offTime\":%ld}", 
             timerActive ? "true" : "false", (long)timerOnTime, (long)timerOffTime);
    mqttClient.publish(topicStateTimer, payload, true);
}

void mqttCallback(char* topic, byte* payload, unsigned int length)
{
    // MEMORY FIX: Hard bound the payload buffer to prevent VLA Stack Overflow
    const unsigned int MAX_PAYLOAD_SIZE = 128;
    
    if (length >= MAX_PAYLOAD_SIZE) {
        Serial.println("MQTT Drop: Payload exceeds safe memory bounds.");
        return; 
    }

    char msg[MAX_PAYLOAD_SIZE];
    memcpy(msg, payload, length);
    msg[length] = '\0';

    Serial.printf("MQTT Rx [%s]: %s\n", topic, msg);

    char* subTopic = strrchr(topic, '/');
    if (subTopic == NULL) return;
    subTopic++; 

    if (strcmp(subTopic, "relay") == 0)
    {
        if (strcasecmp(msg, "ON") == 0 || strcasecmp(msg, "1") == 0) setRelayState(true, "MQTT");
        else if (strcasecmp(msg, "OFF") == 0 || strcasecmp(msg, "0") == 0) setRelayState(false, "MQTT");
    }
    else if (strcmp(subTopic, "reset") == 0)
    {
        if (strcasecmp(msg, "1") == 0 || strcasecmp(msg, "true") == 0)
        {
            setRelayState(false, "ServerReset");
            pzem.resetEnergy();
            isFaultDetected = false;
            currentFault = FAULT_NONE;
            faultStartTime = 0;
            timerActive = false;
            publishFaultState();
            publishRelayState();
            publishTimerState();
            Serial.println(">> Fault state reset via MQTT.");
        }
    }
    else if (strcmp(subTopic, "timer") == 0)
    {
        if (strcasecmp(msg, "OFF") == 0)
        {
            timerActive = false;
            publishTimerState();
            Serial.println(">> Timer disabled via MQTT.");
        }
        else
        {
            long onT, offT;
            if (sscanf(msg, "%ld,%ld", &onT, &offT) == 2)
            {
                time_t now;
                time(&now);
                if (onT > now && offT > onT)
                {
                    timerOnTime = onT;
                    timerOffTime = offT;
                    timerActive = true;
                    publishTimerState();
                    Serial.println(">> Timer updated via MQTT.");
                }
                else Serial.println(">> Invalid timer timestamps received.");
            }
        }
    }
    else if (strcmp(subTopic, "config") == 0)
    {
        float oc, uv;
        int freq;
        if (sscanf(msg, "%f,%f,%d", &oc, &uv, &freq) == 3)
        {
            limitOverCurrent = oc;
            limitUnderVoltage = uv;
            limitExpectedFreq = freq;
            Serial.printf(">> Config updated: OC=%.1fA, UV=%.1fV, Freq=%dHz\n", oc, uv, freq);
        }
    }
}

void checkLocalTime()
{
    struct tm timeinfo;
    if (!getLocalTime(&timeinfo))
    {
        Serial.println("Failed to obtain time");
        return;
    }
    char timeStr[64];
    strftime(timeStr, sizeof(timeStr), "%A, %B %d %Y %H:%M:%S", &timeinfo);
    Serial.println(timeStr);
}

void timeAvailable(struct timeval *t)
{
    checkLocalTime();
    Serial.println("System time updated from NTP successfully!");
}

void forceTimeSync()
{
    Serial.println("Forcing NTP sync...");
    if (WiFi.status() != WL_CONNECTED) return;

    sntp_set_sync_interval(3600000); 
    sntp_set_time_sync_notification_cb(timeAvailable);
    configTzTime(time_zone, ntpServer1, ntpServer2, ntpServer3);

    unsigned long startSync = millis();
    while (sntp_get_sync_status() != SNTP_SYNC_STATUS_COMPLETED && (millis() - startSync < 15000))
    {
        delay(500);
        Serial.print(".");
    }
    Serial.print("\nSync NTP complete or timed out.\n");
}

void resolveHostToBuffer(const char* hostname, char* buffer, size_t len)
{
    IPAddress remote_ip;
    if (WiFi.hostByName(hostname, remote_ip)) 
    {
        // MEMORY FIX: Prevent dynamic String creation and heap fragmentation
        snprintf(buffer, len, "%d.%d.%d.%d", remote_ip[0], remote_ip[1], remote_ip[2], remote_ip[3]);
    }
    else 
    {
        strncpy(buffer, "0.0.0.0", len);
    }
}

void forceWifiReconnect()
{
    // Flush secure socket cleanly before toggling radio
    secureClient.stop();
    WiFi.disconnect(true);
    
    WiFi.mode(WIFI_MODE_NULL);
    delay(100);
    WiFi.mode(WIFI_STA); 
    delay(100);
    WiFi.setHostname(hostname);
    WiFi.setAutoReconnect(true);
    WiFi.persistent(true);
    WiFi.begin(ssid, password);
    
    unsigned long startAttempt = millis();
    while (WiFi.status() != WL_CONNECTED && (millis() - startAttempt < 15000))
    {
        delay(500);
        Serial.print(".");
    }
}

void maintainConnections()
{
    if (WiFi.status() != WL_CONNECTED)
    {
        Serial.println("WiFi connection lost. Reconnecting ...");
        
        // MEMORY FIX: Ensure sockets are cleanly destroyed
        if(mqttClient.connected()) {
            mqttClient.disconnect();
        }
        secureClient.stop(); 
        
        forceWifiReconnect();
        if(WiFi.status() == WL_CONNECTED) {
            Serial.println("\nWiFi connection restored!");
        }
        return; 
    }

    if (!mqttClient.connected())
    {
        unsigned long now = millis();
        static unsigned long lastMqttReconnectAttempt = 0;
        if (now - lastMqttReconnectAttempt > 5000) // Throttled to 5 seconds to prevent SSL spam
        {
            lastMqttReconnectAttempt = now;
            Serial.print("Attempting MQTT reconnection...");
            
            if (mqttClient.connect(hostname))
            {
                Serial.println("connected ✅");
                mqttClient.subscribe(topicCmd);
                
                publishRelayState();
                publishFaultState();
                publishTimerState();
            }
            else
            {
                Serial.printf("failed, rc=%d try again in 5 seconds\n", mqttClient.state());
                secureClient.stop(); // Force flush socket on failed SSL handshake
            }
        }
    }
}

void processSerialCommands()
{
    static char cmdBuffer[64];
    static size_t bufferIdx = 0;

    while (Serial.available() > 0)
    {
        char c = Serial.read();

        if (c == '\n' || c == '\r')
        {
            if (bufferIdx > 0)
            {
                cmdBuffer[bufferIdx] = '\0';
                
                if (strcasecmp(cmdBuffer, "relay") == 0)
                {
                    setRelayState(!relayState, "Serial");
                }
                else if (strcasecmp(cmdBuffer, "reset") == 0)
                {
                    setRelayState(false, "SerialReset");
                    pzem.resetEnergy();
                    isFaultDetected = false;
                    currentFault = FAULT_NONE;
                    faultStartTime = 0;
                    timerActive = false;
                    publishFaultState();
                    publishRelayState();
                    publishTimerState();
                    Serial.println(">> System: Fault flags and state reset cleared.");
                }
                else if (strcasecmp(cmdBuffer, "restart") == 0 || strcasecmp(cmdBuffer, "reboot") == 0)
                {
                    Serial.println(">> System: Hardware restart initiated...");
                    delay(500);
                    ESP.restart();
                }
                else
                {
                    Serial.printf("Unknown command: '%s'. Available: relay, reset, restart\n", cmdBuffer);
                }
                bufferIdx = 0; 
            }
        }
        else
        {
            if (bufferIdx < sizeof(cmdBuffer) - 1)
            {
                cmdBuffer[bufferIdx++] = c;
            }
        }
    }
}
