#include <Wire.h>
#include <Adafruit_ADS1X15.h>
#include <Preferences.h>
#include <driver/ledc.h>
#include <BLEDevice.h>
#include <BLEUtils.h>
#include <BLEServer.h>
#include <BLE2902.h>
#include <HardwareSerial.h>
#include <esp_efuse.h>
#include <esp_mac.h>

#define _USE_MATH_DEFINES
#include <math.h>

/* ============================================
   BLE CONFIGURATION
   ============================================ */
#define SERVICE_UUID     "0000ffe0-0000-1000-8000-00805f9b34fb"
#define LIVE_CHAR_UUID   "0000ffe4-0000-1000-8000-00805f9b34fb"
#define CONFIG_CHAR_UUID "0000ffe5-0000-1000-8000-00805f9b34fb"
#define ADV_CHAR_UUID    "0000ffe6-0000-1000-8000-00805f9b34fb"
#define AUTH_PASSWORD    "SERVO_EVDR_2026"
#define FW_VERSION       "FINAL_BLE_UART_CODE_v1.6_uart_rx_fix"

/* ============================================
   PIN CONFIGURATION
   ============================================ */
namespace Pins {
    const uint8_t I2C_SDA = 8;
    const uint8_t I2C_SCL = 9;
    const uint8_t PWM_PIN = 10;
    const uint8_t MCU_D2 = 3;
    const uint8_t MCU_D1 = 4;
    const uint8_t MCU_D0 = 5;
    // Keep SAT LED off UART pins. RX is GPIO6, TX is GPIO7.
    const uint8_t SAT_LED = 2;
    const uint8_t STATUS_LED = 2;
}

/* ============================================
   UART CABLE CONFIGURATION (HALF-DUPLEX)
   ============================================ */
namespace UARTConfig {
    const uint8_t UART_PORT = 0;
    const uint32_t BAUD = 9600;
    // Keep these separate from control pins used above.
    const uint8_t RX_PIN = 6;
    const uint8_t TX_PIN = 7;
    const unsigned long LOOPBACK_IGNORE_MS = 80;
    const size_t MAX_CMD_LEN = 768; // Allow full JSON update payloads over UART.
}

namespace DebugConfig {
    // Keep verbose traffic logs OFF in production; they can disturb long UART command handling.
    const bool SERIAL_TRAFFIC = false;
    const bool STATUS_STREAM = false;
}

/* ============================================
   PWM CONFIGURATION
   ============================================ */
namespace PWMConfig {
    const uint32_t PWM_FREQ = 4000;
    const uint32_t PWM_RESOLUTION = 12;
    const uint32_t PWM_DUTY_MAX = (1 << PWM_RESOLUTION) - 1;
}

/* ============================================
   ADS1115 CONFIGURATION
   ============================================ */
Adafruit_ADS1115 ads;
const uint8_t ADS1115_ADDRESS = 0x48;

namespace Constants {
    const float ADS1115_MAX_VALUE = 32767.0;
    const float ADS1115_VOLTAGE_RANGE = 4.096;
    const float I_MIN = 0.0;
    const float I_MAX = 2000.0;
    const float DUTY_MIN = 0.03;
    const float DUTY_MAX = 0.98;
    const float INT_LIMIT = 2000;
    const float ALPHA = 0.02;
    const float MA_SCALE_FACTOR_4_20MA = 1.022;
    const float RESISTANCE_SCALE_FACTOR = 1.04;
    const float VOLTAGE_SCALE_FACTOR = 0.9947;
    const float FB_TO_CURRENT_FACTOR = 1.630;
    const float FB_OFFSET = 1361.0;
    const float VCMD_DEADBAND_MV = 25.0;  
}

namespace UpdateSafety {
    // During config/advanced updates, command current transitions are intentionally
    // slowed to avoid abrupt current drops/rises on the connected load.
    const float NORMAL_SLEW_UP_MA_PER_S = 12000.0f;
    const float NORMAL_SLEW_DOWN_MA_PER_S = 12000.0f;
    const float UPDATE_SLEW_UP_MA_PER_S = 1200.0f;
    const float UPDATE_SLEW_DOWN_MA_PER_S = 700.0f;
    const unsigned long UPDATE_SMOOTH_WINDOW_MS = 2600UL;
    // After virtual-0V window ends, keep slew limited for smooth return to live input.
    const unsigned long UPDATE_RECOVERY_SLEW_MS = 1800UL;
}

/* ============================================
   NON-VOLATILE STORAGE
   ============================================ */
struct Configuration {
    uint8_t mode;
    float scale_in_min, scale_in_max;
    float scale_out_min, scale_out_max;
    int bp_count;
    float bp_in[5];
    float bp_out[5];
    float Kp, Ki;
    float dither_freq, dither_ampl;
    bool dither_enable;
    uint32_t magic_number;
};

Preferences preferences;
Configuration settings;
const uint32_t MAGIC_ID = 0xDEADBEEF;

/* ============================================
   GLOBAL VARIABLES
   ============================================ */
String lastConfig = "";
String lastAdvanced = "";
String deviceName = "";

/* ============================================
   BLE GLOBAL VARIABLES
   ============================================ */
BLEServer *pServer = nullptr;
BLECharacteristic *pLiveChar = nullptr;
BLECharacteristic *pConfigChar = nullptr;
BLECharacteristic *pAdvChar = nullptr;
bool deviceConnected = false;
bool oldDeviceConnected = false;
bool isAuthenticated = false;
unsigned long lastBLENotify = 0;
bool pendingConfigSave = false;
bool pendingAdvancedSave = false;
bool bleAdvertisingEnabled = true;

/* ============================================
   UART GLOBAL VARIABLES
   ============================================ */
HardwareSerial cableUART(UARTConfig::UART_PORT);
String rxBuffer = "";
bool ignoreRx = false;
unsigned long ignoreUntil = 0;
bool uartAuthenticated = false;
bool uartSessionForced = false;
bool uartSessionActive = false;
unsigned long lastUARTActivity = 0;
constexpr unsigned long UART_SESSION_IDLE_MS = 6000;
unsigned long updateSmoothUntilMs = 0;
unsigned long updateSlewUntilMs = 0;

/* ============================================
   RUNTIME VARIABLES
   ============================================ */
struct RuntimeData {
    float integral;
    float duty_p;
    float Vcmd_f;
    float Vfb_f;
    float Vcmd_raw;
    float Vfb_raw;
    float Vcmd_f_v;
    float Vfb_f_v;
    int16_t Vcmd_adc;
    int16_t Vfb_adc;
    float inputPhysical_global;
    float Icmd_target;
    float Icmd;
    float Icmd_slew;
    float Iout;
    float duty_cycle;
    unsigned long lastIcmdSlewUpdateUs;
    unsigned long lastDitherUpdate;
    float current_dither;
    float dither_angle;
    float temperature;
    float supplyVoltage;
    bool faultStatus;
    unsigned long lastPrint;
    String inputString;
};

RuntimeData runtime = {0};

/* ============================================
   FORWARD DECLARATIONS
   ============================================ */
void setupPWM();
void saveSettings();
void loadSettings();
void setMode(uint8_t mode);
float computeDither();
float applyBreakpoints(float input);
float computeCommandCurrent(float Vcmd_mV);
float computeCurrentFeedback(float Vfb_mV);
void processSerial();
void printStatus();
void resetDitherState();
void sendBLETelemetry();
void sendBLEConfigResponse(const String &response);
void sendBLEAdvResponse(const String &response);
void blinkLED(int times, int delayMs);
void saveConfigToEEPROM();
void saveAdvancedToEEPROM();
void loadParametersFromEEPROM();
void updateControlLoop();
void markUpdateSmoothWindow();
bool isUpdateSmoothActive();
bool isUpdateSlewLimitedActive();
float applyCommandSlew(float targetCurrent);
String getModeString(uint8_t mode);
void parseAndApplyConfig(String json);
void parseAndApplyAdvanced(String json);
void setupUART();
void processUART();
void handleUARTCommand(const String &cmd);
void handleUpdate(String cmd);
void startIgnoreRx(unsigned long ms);
void updateIgnoreRx();
void sendLine(String msg);
void sendValues();
String buildTelemetryPayload();
void markUARTActivity();
void setUARTSessionForced(bool active);
bool isUARTSessionActive();
void updateBLEAvailability();
bool processUARTSessionControlCommand(const String &cmd, bool viaUSBSerial);

/* ============================================
   STORAGE FUNCTIONS
   ============================================ */
void saveConfigToEEPROM() {
    preferences.begin("servo_params", false);
    preferences.putString("config", lastConfig);
    preferences.end();
    Serial.println("Config saved to EEPROM");
}

void saveAdvancedToEEPROM() {
    preferences.begin("servo_params", false);
    preferences.putString("advanced", lastAdvanced);
    preferences.end();
    Serial.println("Advanced saved to EEPROM");
}

void loadParametersFromEEPROM() {
    preferences.begin("servo_params", true);

    lastConfig = preferences.getString(
        "config",
        "CFG:{\"mode\":\"0-10V\",\"input\":[0,10],\"output\":[0,2000],\"unit\":\"mA\",\"points\":[]}"
    );

    lastAdvanced = preferences.getString(
        "advanced",
        "ADV:{\"kp\":0.15,\"ki\":0.00005,\"ditherEnable\":1,\"ditherFreq\":150,\"ditherAmplitude\":0.05,\"pwmFreq\":2000}"
    );

    preferences.end();
    Serial.println("Parameters loaded from EEPROM");
}

void markUARTActivity() {
    lastUARTActivity = millis();
    if (!uartSessionActive) {
        uartSessionActive = true;
        Serial.println("UART session active -> BLE disabled");
    }
}

void setUARTSessionForced(bool active) {
    uartSessionForced = active;
    if (active) {
        markUARTActivity();
    } else {
        uartSessionActive = false;
        lastUARTActivity = 0;
        Serial.println("UART session released -> BLE can resume");
    }
}

bool isUARTSessionActive() {
    if (uartSessionForced) {
        return true;
    }
    if (!uartSessionActive) {
        return false;
    }
    if ((millis() - lastUARTActivity) > UART_SESSION_IDLE_MS) {
        uartSessionActive = false;
        return false;
    }
    return true;
}

void updateBLEAvailability() {
    bool shouldEnableBLE = !isUARTSessionActive();
    if (shouldEnableBLE == bleAdvertisingEnabled) {
        return;
    }

    bleAdvertisingEnabled = shouldEnableBLE;
    isAuthenticated = false;

    BLEAdvertising *pAdvertising = BLEDevice::getAdvertising();
    if (bleAdvertisingEnabled) {
        if (pAdvertising != nullptr) {
            pAdvertising->start();
        } else {
            BLEDevice::startAdvertising();
        }
        Serial.println("BLE enabled: UART disconnected/idle");
        return;
    }

    if (pAdvertising != nullptr) {
        pAdvertising->stop();
    }
    Serial.println("BLE disabled: UART communication active");
}

bool processUARTSessionControlCommand(const String &cmd, bool viaUSBSerial) {
    if (cmd.equalsIgnoreCase("uart_connected")) {
        if (deviceConnected) {
            if (viaUSBSerial) {
                Serial.println("uart_blocked_ble_active");
            } else {
                sendLine("uart_blocked_ble_active");
            }
            Serial.println("UART connect request rejected: BLE is connected");
            return true;
        }

        setUARTSessionForced(true);
        if (viaUSBSerial) {
            Serial.println("uart_mode_on");
        } else {
            sendLine("uart_mode_on");
        }
        return true;
    }

    if (cmd.equalsIgnoreCase("uart_disconnected")) {
        setUARTSessionForced(false);
        if (viaUSBSerial) {
            Serial.println("uart_mode_off");
        } else {
            sendLine("uart_mode_off");
        }
        return true;
    }

    return false;
}

String getModeString(uint8_t mode) {
    switch(mode) {
        case 1: return "Resistance (0-10)";
        case 2: return "0-5V";
        case 3: return "0-10V";
        case 4: return "0-20mA";
        default: return "0-10V";
    }
}

void parseAndApplyConfig(String json) {
    Serial.println(">>> Parsing New Config...");
    Serial.print("Raw JSON: "); Serial.println(json);
    markUpdateSmoothWindow();
    
    // Parse mode
    if (json.indexOf("\"mode\"") > 0) {
        int start = json.indexOf("\"mode\"") + 6;
        start = json.indexOf("\"", start) + 1;
        int end = json.indexOf("\"", start);
        String modeStr = json.substring(start, end);
        modeStr.trim();
        
        if (modeStr == "0-5V") setMode(2);
        else if (modeStr == "0-10V") setMode(3);
        else if (modeStr == "0-20mA") setMode(4);
        else if (modeStr == "Resistance (0-10)") setMode(1);
        Serial.print("  Mode: "); Serial.println(modeStr);
    }
    
    // Parse input range
    if (json.indexOf("\"input\"") > 0) {
        int start = json.indexOf("[", json.indexOf("\"input\"")) + 1;
        int comma = json.indexOf(",", start);
        int end = json.indexOf("]", start);
        if (comma > 0 && end > comma) {
            settings.scale_in_min = json.substring(start, comma).toFloat();
            settings.scale_in_max = json.substring(comma + 1, end).toFloat();
            Serial.print("  Input Range: "); Serial.print(settings.scale_in_min); 
            Serial.print(" to "); Serial.println(settings.scale_in_max);
        }
    }
    
    // Parse output range
    if (json.indexOf("\"output\"") > 0) {
        int start = json.indexOf("[", json.indexOf("\"output\"")) + 1;
        int comma = json.indexOf(",", start);
        int end = json.indexOf("]", start);
        if (comma > 0 && end > comma) {
            settings.scale_out_min = json.substring(start, comma).toFloat();
            settings.scale_out_max = json.substring(comma + 1, end).toFloat();
            Serial.print("  Output Range: "); Serial.print(settings.scale_out_min); 
            Serial.print(" to "); Serial.println(settings.scale_out_max);
        }
    }
    
    // Parse breakpoints
    if (json.indexOf("\"points\"") > 0) {
        int pointsArrayStart = json.indexOf("[", json.indexOf("\"points\""));
        String pointsStr = json.substring(pointsArrayStart);
        
        int bpIdx = 0;
        int pos = 1; // Skip outer [
        while (bpIdx < 5) {
            int start = pointsStr.indexOf("[", pos);
            int end = pointsStr.indexOf("]", start + 1);
            if (start < 0 || end < 0) break;
            
            // Check if this [ ] is actually a pair (not the end of outer array)
            if (start > pointsStr.lastIndexOf("]")) break;

            String point = pointsStr.substring(start + 1, end);
            int comma = point.indexOf(",");
            if (comma > 0) {
                settings.bp_in[bpIdx] = point.substring(0, comma).toFloat();
                settings.bp_out[bpIdx] = point.substring(comma + 1).toFloat();
                bpIdx++;
            }
            pos = end + 1;
        }
        settings.bp_count = bpIdx;
        Serial.print("  Total Breakpoints Applied: "); Serial.println(settings.bp_count);
    }
    pendingConfigSave = true;
}

void parseAndApplyAdvanced(String json) {
    markUpdateSmoothWindow();
    if (json.indexOf("\"kp\"") > 0) {
        int kpStart = json.indexOf("\"kp\"") + 5;
        int kpEnd = json.indexOf(",", kpStart);
        if (kpEnd < 0) kpEnd = json.indexOf("}", kpStart);
        settings.Kp = json.substring(kpStart, kpEnd).toFloat();
    }
    
    if (json.indexOf("\"ki\"") > 0) {
        int kiStart = json.indexOf("\"ki\"") + 5;
        int kiEnd = json.indexOf(",", kiStart);
        if (kiEnd < 0) kiEnd = json.indexOf("}", kiStart);
        settings.Ki = json.substring(kiStart, kiEnd).toFloat();
    }
    
    if (json.indexOf("\"ditherEnable\"") > 0) {
        int deStart = json.indexOf("\"ditherEnable\"") + 15;
        int deEnd = json.indexOf(",", deStart);
        if (deEnd < 0) deEnd = json.indexOf("}", deStart);
        settings.dither_enable = (json.substring(deStart, deEnd).toInt() == 1);
    }
    
    if (json.indexOf("\"ditherFreq\"") > 0) {
        int dfStart = json.indexOf("\"ditherFreq\"") + 13;
        int dfEnd = json.indexOf(",", dfStart);
        if (dfEnd < 0) dfEnd = json.indexOf("}", dfStart);
        settings.dither_freq = json.substring(dfStart, dfEnd).toFloat();
    }
    
    if (json.indexOf("\"ditherAmplitude\"") > 0) {
        int daStart = json.indexOf("\"ditherAmplitude\"") + 18;
        int daEnd = json.indexOf(",", daStart);
        if (daEnd < 0) daEnd = json.indexOf("}", daStart);
        settings.dither_ampl = json.substring(daStart, daEnd).toFloat();
    }
    
    resetDitherState();
    
    // Reset integral to avoid control spike when gain changes
    runtime.integral = 0;
    pendingAdvancedSave = true;
}

/* ============================================
   BLE CALLBACKS
   ============================================ */
class MyServerCallbacks : public BLEServerCallbacks {
    void onConnect(BLEServer *server) override {
        deviceConnected = true;
        isAuthenticated = false;
        Serial.println("✓ BLE Client Connected");
        blinkLED(2, 120);
    }

    void onDisconnect(BLEServer *server) override {
        deviceConnected = false;
        isAuthenticated = false;
        Serial.println("✗ BLE Client Disconnected");
        blinkLED(1, 200);
    }
};

class ConfigCallbacks : public BLECharacteristicCallbacks {
    void onWrite(BLECharacteristic *pCharacteristic) override {
        String rx = String(pCharacteristic->getValue().c_str());
        rx.trim();

        if (isUARTSessionActive()) {
            sendBLEConfigResponse("ERROR: UART active");
            Serial.println("BLE CONFIG blocked: UART active");
            return;
        }

        Serial.print("CONFIG WRITE: ");
        Serial.println(rx);

        // Handle AUTH
        if (rx.startsWith("AUTH:")) {
            String password = rx.substring(5);
            password.trim();

            if (password == AUTH_PASSWORD) {
                isAuthenticated = true;
                sendBLEConfigResponse("AUTH_SUCCESS");
                Serial.println("✓ AUTH SUCCESS");
                blinkLED(3, 50);
            } else {
                isAuthenticated = false;
                sendBLEConfigResponse("AUTH_FAILED");
                Serial.println("✗ AUTH FAILED");
                blinkLED(1, 120);
            }
            return;
        }

        // Handle GET - Load configuration from device
        if (rx.equalsIgnoreCase("GET")) {
            if (!isAuthenticated) {
                sendBLEConfigResponse("ERROR: Authentication required");
                Serial.println("GET REJECTED: not authenticated");
                return;
            }
            
            // Send stored configuration
            sendBLEConfigResponse(lastConfig);
            Serial.println("✓ CONFIG SENT to client");
            return;
        }

        // Handle CFG - Save configuration
        if (rx.startsWith("CFG:")) {
            if (!isAuthenticated) {
                sendBLEConfigResponse("ERROR: Authentication required");
                Serial.println("CFG REJECTED: not authenticated");
                return;
            }

            lastConfig = rx;
            pendingConfigSave = true;
            
            // Parse and apply configuration
            String json = rx.substring(4);
            parseAndApplyConfig(json);
            
            // Reset PID integrator so stale error from old scale doesn't cause
            // a transient that looks like doubled output current
            runtime.integral = 0;
            runtime.duty_p = constrain(runtime.duty_p, Constants::DUTY_MIN, Constants::DUTY_MAX);
            
            sendBLEConfigResponse("OK");
            Serial.println("✓ CONFIG RECEIVED & APPLIED");
            blinkLED(3, 70);
            return;
        }

        sendBLEConfigResponse("ERROR: Unknown command");
    }

    void onRead(BLECharacteristic *pCharacteristic) override {
        Serial.println("CONFIG READ REQUESTED");
        pCharacteristic->setValue(lastConfig.c_str());
    }
};

class AdvancedCallbacks : public BLECharacteristicCallbacks {
    void onWrite(BLECharacteristic *pCharacteristic) override {
        String rx = String(pCharacteristic->getValue().c_str());
        rx.trim();

        if (isUARTSessionActive()) {
            sendBLEAdvResponse("ERROR: UART active");
            Serial.println("BLE ADV blocked: UART active");
            return;
        }

        Serial.print("ADV WRITE: ");
        Serial.println(rx);

        if (!isAuthenticated) {
            sendBLEAdvResponse("ERROR: Authentication required");
            Serial.println("ADV REJECTED: not authenticated");
            return;
        }

        // Handle GET for advanced
        if (rx.equalsIgnoreCase("GET")) {
            sendBLEAdvResponse(lastAdvanced);
            Serial.println("✓ ADVANCED SENT to client");
            return;
        }

        // Handle ADV - Save advanced settings
        if (rx.startsWith("ADV:")) {
            lastAdvanced = rx;
            pendingAdvancedSave = true;
            
            // Parse and apply advanced settings
            String json = rx.substring(4);
            parseAndApplyAdvanced(json);
            
            sendBLEAdvResponse("OK");
            Serial.println("✓ ADVANCED RECEIVED & APPLIED");
            blinkLED(4, 50);
            return;
        }

        sendBLEAdvResponse("ERROR: Unknown command");
    }

    void onRead(BLECharacteristic *pCharacteristic) override {
        Serial.println("ADV READ REQUESTED");
        pCharacteristic->setValue(lastAdvanced.c_str());
    }
};

/* ============================================
   BLE HELPER FUNCTIONS
   ============================================ */
void sendBLEConfigResponse(const String &response) {
    if (pConfigChar != nullptr && deviceConnected && !isUARTSessionActive()) {
        pConfigChar->setValue(response.c_str());
        pConfigChar->notify();
        Serial.println("BLE Response: " + response);
        delay(20);
    }
}

void sendBLEAdvResponse(const String &response) {
    if (pAdvChar != nullptr && deviceConnected && !isUARTSessionActive()) {
        pAdvChar->setValue(response.c_str());
        pAdvChar->notify();
        Serial.println("BLE Adv Response: " + response);
        delay(20);
    }
}

String buildTelemetryPayload() {
    // Keep BLE and UART telemetry identical.
    runtime.temperature = 25.0 + (runtime.Iout / 2000.0) * 15.0;
    runtime.supplyVoltage = 24.0 - (runtime.duty_cycle * 1.5);
    runtime.faultStatus = (runtime.Iout > 1950);

    float safeIout = (isnan(runtime.Iout) || isinf(runtime.Iout)) ? 0.0f : constrain(runtime.Iout, 0, Constants::I_MAX);
    float safeIcmd = (isnan(runtime.Icmd) || isinf(runtime.Icmd)) ? 0.0f : constrain(runtime.Icmd, 0, Constants::I_MAX);

    char payload[256];
    snprintf(payload, sizeof(payload),
        "input:%.2f|output:%.0f|command:%.0f|temp:%.1f|supply:%.1f|fault:%d|sat:%d",
        runtime.inputPhysical_global,
        safeIout,
        safeIcmd,
        runtime.temperature,
        runtime.supplyVoltage,
        runtime.faultStatus ? 1 : 0,
        (runtime.inputPhysical_global >= settings.scale_in_max) ? 1 : 0
    );

    return String(payload);
}

void sendBLETelemetry() {
    if (!deviceConnected || isUARTSessionActive()) return;

    String payload = buildTelemetryPayload();
    pLiveChar->setValue(payload.c_str());
    pLiveChar->notify();
}

void blinkLED(int times, int delayMs) {
    for (int i = 0; i < times; i++) {
        digitalWrite(Pins::STATUS_LED, HIGH);
        delay(delayMs);
        digitalWrite(Pins::STATUS_LED, LOW);
        delay(delayMs);
    }
}

void setupBLE() {
    uint8_t mac[6];
    esp_efuse_mac_get_default(mac);
    char bleDeviceName[32];
    snprintf(bleDeviceName, sizeof(bleDeviceName), "SERVO_EVDR_%02X%02X", mac[4], mac[5]);
    deviceName = String(bleDeviceName);
    
    BLEDevice::init(deviceName.c_str());
    BLEDevice::setMTU(512);
    pServer = BLEDevice::createServer();
    pServer->setCallbacks(new MyServerCallbacks());
    
    BLEService *pService = pServer->createService(SERVICE_UUID);
    
    pLiveChar = pService->createCharacteristic(
        LIVE_CHAR_UUID,
        BLECharacteristic::PROPERTY_NOTIFY | BLECharacteristic::PROPERTY_READ
    );
    pLiveChar->addDescriptor(new BLE2902());
    
    pConfigChar = pService->createCharacteristic(
        CONFIG_CHAR_UUID,
        BLECharacteristic::PROPERTY_READ |
        BLECharacteristic::PROPERTY_WRITE |
        BLECharacteristic::PROPERTY_NOTIFY
    );
    pConfigChar->setCallbacks(new ConfigCallbacks());
    pConfigChar->addDescriptor(new BLE2902());
    pConfigChar->setValue("READY");
    
    pAdvChar = pService->createCharacteristic(
        ADV_CHAR_UUID,
        BLECharacteristic::PROPERTY_READ |
        BLECharacteristic::PROPERTY_WRITE |
        BLECharacteristic::PROPERTY_NOTIFY
    );
    pAdvChar->setCallbacks(new AdvancedCallbacks());
    pAdvChar->addDescriptor(new BLE2902());
    
    pService->start();
    
    BLEAdvertising *pAdvertising = BLEDevice::getAdvertising();
    pAdvertising->addServiceUUID(SERVICE_UUID);
    pAdvertising->setScanResponse(true);
    BLEDevice::startAdvertising();
    
    Serial.print("BLE Device Name: ");
    Serial.println(deviceName);
    Serial.println("BLE Advertising Started");
    Serial.print("Auth Password: ");
    Serial.println(AUTH_PASSWORD);
}

/* ============================================
   UART HELPER FUNCTIONS (HALF-DUPLEX)
   ============================================ */
void startIgnoreRx(unsigned long ms) {
    ignoreRx = true;
    ignoreUntil = millis() + ms;
}

void updateIgnoreRx() {
    if (ignoreRx && millis() > ignoreUntil) {
        ignoreRx = false;

        // clear anything looped back during transmit
        while (cableUART.available()) {
            cableUART.read();
        }
    }
}

void sendLine(String msg) {
    // Ignore RX long enough for full TX frame to avoid self-loopback parsing.
    // 8N1 ~= 10 bits/char. Add CRLF from println (+2 chars) and safety margin.
    const unsigned long charsOnWire = (unsigned long)msg.length() + 2UL;
    const unsigned long txTimeMs = (charsOnWire * 10UL * 1000UL + (UARTConfig::BAUD - 1UL)) / UARTConfig::BAUD;
    unsigned long ignoreMs = txTimeMs + 25UL;
    if (ignoreMs < UARTConfig::LOOPBACK_IGNORE_MS) {
        ignoreMs = UARTConfig::LOOPBACK_IGNORE_MS;
    }

    startIgnoreRx(ignoreMs);
    cableUART.println(msg);
}

void sendValues() {
    // Keep cable output aligned with BLE live reading payload format.
    String data = buildTelemetryPayload();
    sendLine(data);
}

void setupUART() {
    cableUART.begin(UARTConfig::BAUD, SERIAL_8N1, UARTConfig::RX_PIN, UARTConfig::TX_PIN);
    delay(300);

    while (cableUART.available()) {
        cableUART.read();
    }

    Serial.println("SERVO Device Ready");
    Serial.print("UART Data-Line Config -> Port: ");
    Serial.print(UARTConfig::UART_PORT);
    Serial.print(" | RX Pin: ");
    Serial.print(UARTConfig::RX_PIN);
    Serial.print(" | TX Pin: ");
    Serial.print(UARTConfig::TX_PIN);
    Serial.print(" | Baud: ");
    Serial.println(UARTConfig::BAUD);
    Serial.print("Control Pins -> D2: ");
    Serial.print(Pins::MCU_D2);
    Serial.print(" D1: ");
    Serial.print(Pins::MCU_D1);
    Serial.print(" D0: ");
    Serial.println(Pins::MCU_D0);
}

void handleUpdate(String cmd) {
    if (!uartAuthenticated) {
        sendLine("auth_required");
        return;
    }
    // One-time authorization per update action.
    uartAuthenticated = false;

    int colonPos = cmd.indexOf(':');
    if (colonPos == -1) {
        sendLine("update_failed");
        return;
    }

    String payload = cmd.substring(colonPos + 1);
    payload.trim();

    if (payload.startsWith("CFG:")) {
        String json = payload.substring(4);
        lastConfig = "CFG:" + json;
        pendingConfigSave = true;
        parseAndApplyConfig(json);

        // Keep controller behavior same as BLE config update.
        runtime.integral = 0;
        runtime.duty_p = constrain(runtime.duty_p, Constants::DUTY_MIN, Constants::DUTY_MAX);

        sendLine("device updated");
        return;
    }
    else if (payload.startsWith("ADV:")) {
        String json = payload.substring(4);
        lastAdvanced = "ADV:" + json;
        pendingAdvancedSave = true;
        parseAndApplyAdvanced(json);

        sendLine("device updated");
        return;
    }

    sendLine("update_failed");
}

void handleUARTCommand(const String &cmd) {
    if (processUARTSessionControlCommand(cmd, false)) {
        return;
    }

    if (deviceConnected) {
        sendLine("uart_blocked_ble_active");
        return;
    }

    markUARTActivity();

    if (cmd.equals("read")) {
        sendValues();
    }
    else if (cmd.equalsIgnoreCase("devname")) {
        String dn = (deviceName.length() > 0) ? deviceName : "SERVO_EVDR";
        sendLine(String("DEVNAME:") + dn);
    }
    else if (cmd.equalsIgnoreCase("version")) {
        sendLine(String("FW:") + FW_VERSION);
    }
    else if (cmd.equalsIgnoreCase("readcfg")) {
        sendLine(lastConfig);
    }
    else if (cmd.equalsIgnoreCase("readadv")) {
        sendLine(lastAdvanced);
    }
    else if (cmd.startsWith("auth:")) {
        String password = cmd.substring(5);
        password.trim();
        if (password == AUTH_PASSWORD) {
            uartAuthenticated = true;
            sendLine("auth_success");
        } else {
            uartAuthenticated = false;
            sendLine("auth_failed");
        }
    }
    else if (cmd.startsWith("update:")) {
        handleUpdate(cmd);
    }
    else if (cmd.startsWith("CFG:") || cmd.startsWith("ADV:")) {
        // Backward-compatible shortcut for tools that send direct CFG:/ADV: payloads.
        handleUpdate("update:" + cmd);
    }
    else {
        // Recovery path:
        // If leading bytes were lost/noisy on UART, try to salvage embedded CFG:/ADV: payload.
        String upper = cmd;
        upper.toUpperCase();
        int cfgPos = upper.indexOf("CFG:");
        int advPos = upper.indexOf("ADV:");
        int pos = -1;
        if (cfgPos >= 0 && advPos >= 0) pos = (cfgPos < advPos) ? cfgPos : advPos;
        else if (cfgPos >= 0) pos = cfgPos;
        else if (advPos >= 0) pos = advPos;

        if (pos >= 0) {
            String recovered = cmd.substring(pos);
            recovered.trim();
            handleUpdate("update:" + recovered);
            return;
        }

        sendLine("unknown_command");
    }
}

void processUART() {
    updateIgnoreRx();

    if (ignoreRx) {
        return;
    }

    while (cableUART.available()) {
        char c = cableUART.read();

        if (DebugConfig::SERIAL_TRAFFIC) {
            Serial.print("RX CHAR: ");
            Serial.println((int)c);
        }

        if (c == '\r') {
            continue;
        }

        if (c == '\n') {
            rxBuffer.trim();

            if (DebugConfig::SERIAL_TRAFFIC) {
                Serial.print("FULL CMD: ");
                Serial.println(rxBuffer);
            }

            if (rxBuffer.length() > 0) {
                handleUARTCommand(rxBuffer);
            }

            rxBuffer = "";
        } else {
            rxBuffer += c;

            if (rxBuffer.length() > UARTConfig::MAX_CMD_LEN) {
                rxBuffer = "";
                sendLine("buffer_cleared");
            }
        }
    }
}

/* ============================================
   CONTROLLER FUNCTIONS
   ============================================ */
String getInputUnit(uint8_t mode) {
    switch(mode) {
        case 1: return "KΩ";
        case 2: return "V";
        case 3: return "V";
        case 4: return "mA";
        default: return "";
    }
}

float readADS1115Voltage(uint8_t channel, int16_t &rawValue) {
    rawValue = ads.readADC_SingleEnded(channel);
    float voltage = (rawValue / Constants::ADS1115_MAX_VALUE) * Constants::ADS1115_VOLTAGE_RANGE;
    return voltage;
}

float lowPassFilter(float prev, float in) {
    return prev + Constants::ALPHA * (in - prev);
}

void markUpdateSmoothWindow() {
    unsigned long nowMs = millis();
    updateSmoothUntilMs = nowMs + UpdateSafety::UPDATE_SMOOTH_WINDOW_MS;
    updateSlewUntilMs = updateSmoothUntilMs + UpdateSafety::UPDATE_RECOVERY_SLEW_MS;
}

bool isUpdateSmoothActive() {
    return (updateSmoothUntilMs != 0UL) && (millis() < updateSmoothUntilMs);
}

bool isUpdateSlewLimitedActive() {
    return (updateSlewUntilMs != 0UL) && (millis() < updateSlewUntilMs);
}

float applyCommandSlew(float targetCurrent) {
    targetCurrent = constrain(targetCurrent, Constants::I_MIN, Constants::I_MAX);

    unsigned long nowUs = micros();
    if (runtime.lastIcmdSlewUpdateUs == 0UL) {
        runtime.lastIcmdSlewUpdateUs = nowUs;
        runtime.Icmd_slew = targetCurrent;
        return runtime.Icmd_slew;
    }

    float dt = (nowUs - runtime.lastIcmdSlewUpdateUs) / 1000000.0f;
    runtime.lastIcmdSlewUpdateUs = nowUs;
    if (dt <= 0.0f || dt > 0.25f) {
        dt = 0.01f;
    }

    const bool smoothActive = isUpdateSlewLimitedActive();
    const float upRate = smoothActive ? UpdateSafety::UPDATE_SLEW_UP_MA_PER_S : UpdateSafety::NORMAL_SLEW_UP_MA_PER_S;
    const float downRate = smoothActive ? UpdateSafety::UPDATE_SLEW_DOWN_MA_PER_S : UpdateSafety::NORMAL_SLEW_DOWN_MA_PER_S;
    const float maxRise = upRate * dt;
    const float maxFall = downRate * dt;

    float delta = targetCurrent - runtime.Icmd_slew;
    if (delta > maxRise) {
        delta = maxRise;
    } else if (delta < -maxFall) {
        delta = -maxFall;
    }

    runtime.Icmd_slew = constrain(runtime.Icmd_slew + delta, Constants::I_MIN, Constants::I_MAX);
    return runtime.Icmd_slew;
}

void saveSettings() {
    settings.magic_number = MAGIC_ID;
    preferences.begin("settings", false);
    preferences.putBytes("config", &settings, sizeof(Configuration));
    preferences.end();
    Serial.println("Settings saved to Flash");
}

void loadSettings() {
    preferences.begin("settings", true);
    size_t size = preferences.getBytesLength("config");
    bool settings_loaded = false;
    
    if(size == sizeof(Configuration)) {
        preferences.getBytes("config", &settings, sizeof(Configuration));
        preferences.end();
        
        if(settings.magic_number == MAGIC_ID) {
            Serial.println("Settings loaded from Flash");
            setMode(settings.mode);
            settings_loaded = true;
        }
    }
    
    if(!settings_loaded) {
        preferences.end();
        settings.mode = 3;
        settings.scale_in_min = 0;
        settings.scale_in_max = 10;
        settings.scale_out_min = 0;
        settings.scale_out_max = 2000;
        settings.Kp = 0.15;
        settings.Ki = 0.00005;
        settings.dither_freq = 150;
        settings.dither_ampl = 0.05;
        settings.dither_enable = true;
        settings.bp_count = 0;
        settings.magic_number = MAGIC_ID;
        saveSettings();
        Serial.println("Using default settings");
    }
    
    if(!settings.dither_enable) {
        resetDitherState();
        runtime.current_dither = 0;
    }
}

void setupPWM() {
    ledc_timer_config_t ledc_timer = {
        .speed_mode = LEDC_LOW_SPEED_MODE,
        .duty_resolution = LEDC_TIMER_12_BIT,
        .timer_num = LEDC_TIMER_0,
        .freq_hz = PWMConfig::PWM_FREQ,
        .clk_cfg = LEDC_AUTO_CLK
    };
    ledc_timer_config(&ledc_timer);
    
    ledc_channel_config_t ledc_channel = {
        .gpio_num = Pins::PWM_PIN,
        .speed_mode = LEDC_LOW_SPEED_MODE,
        .channel = LEDC_CHANNEL_0,
        .intr_type = LEDC_INTR_DISABLE,
        .timer_sel = LEDC_TIMER_0,
        .duty = 0,
        .hpoint = 0
    };
    ledc_channel_config(&ledc_channel);
}

void setPWMDutyCycle(float duty_cycle) {
    uint32_t duty_value = (uint32_t)(duty_cycle * PWMConfig::PWM_DUTY_MAX);
    ledc_set_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0, duty_value);
    ledc_update_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0);
}

void setMode(uint8_t mode) {
    settings.mode = mode;
    digitalWrite(Pins::MCU_D2, (mode == 4) ? HIGH : LOW);
    digitalWrite(Pins::MCU_D1, (mode == 1) ? HIGH : LOW);
    digitalWrite(Pins::MCU_D0, (mode == 3) ? HIGH : LOW);
    Serial.print("Mode set to: ");
    Serial.println(mode);
}

void resetDitherState() {
    runtime.lastDitherUpdate = 0;
    runtime.dither_angle = 0;
    runtime.current_dither = 0;
}

float computeDither() {
    if(!settings.dither_enable || settings.dither_ampl <= 0) {
        runtime.current_dither = 0;
        return 0;
    }
    
    unsigned long now = micros();
    
    if(runtime.lastDitherUpdate == 0) {
        runtime.lastDitherUpdate = now;
        runtime.dither_angle = 0;
        return 0;
    }
    
    float deltaTime = (now - runtime.lastDitherUpdate) / 1000000.0;
    runtime.lastDitherUpdate = now;
    
    runtime.dither_angle += 2.0 * M_PI * settings.dither_freq * deltaTime;
    
    if(runtime.dither_angle > 2.0 * M_PI) {
        runtime.dither_angle -= 2.0 * M_PI;
    }
    
    float dither = settings.dither_ampl * sin(runtime.dither_angle);
    runtime.current_dither = dither;
    
    return dither;
}

float applyBreakpoints(float input) {
    // Guard: if range is zero, return min to avoid divide-by-zero
    float inRange = settings.scale_in_max - settings.scale_in_min;
    if(inRange <= 0) return settings.scale_out_min;

    if(settings.bp_count == 0) {
        float fraction = constrain((input - settings.scale_in_min) / inRange, 0, 1);
        return settings.scale_out_min + fraction * (settings.scale_out_max - settings.scale_out_min);
    }
    
    // Before first breakpoint
    if(input <= settings.bp_in[0]) {
        float seg = settings.bp_in[0] - settings.scale_in_min;
        float fraction = (seg > 0) ? constrain((input - settings.scale_in_min) / seg, 0, 1) : 0;
        return settings.scale_out_min + fraction * (settings.bp_out[0] - settings.scale_out_min);
    }
    
    // Between breakpoints
    for(int i = 0; i < settings.bp_count - 1; i++) {
        if(input >= settings.bp_in[i] && input <= settings.bp_in[i+1]) {
            float seg = settings.bp_in[i+1] - settings.bp_in[i];
            if(seg <= 0) return settings.bp_out[i];
            float fraction = (input - settings.bp_in[i]) / seg;
            return settings.bp_out[i] + fraction * (settings.bp_out[i+1] - settings.bp_out[i]);
        }
    }
    
    // After last breakpoint
    float lastSeg = settings.scale_in_max - settings.bp_in[settings.bp_count - 1];
    if(lastSeg <= 0) return settings.bp_out[settings.bp_count - 1]; // last BP == scale_in_max, no trailing segment
    float fraction = constrain((input - settings.bp_in[settings.bp_count - 1]) / lastSeg, 0, 1);
    return settings.bp_out[settings.bp_count - 1] + 
           fraction * (settings.scale_out_max - settings.bp_out[settings.bp_count - 1]);
}

float computeCommandCurrent(float Vcmd_mV) {
    float Vcmd_processed = (Vcmd_mV < Constants::VCMD_DEADBAND_MV) ? 0.0 : Vcmd_mV;
    float scale = Vcmd_processed / 3300.0;
    
    if(settings.mode == 2) {
        runtime.inputPhysical_global = scale * 5;
    } else if(settings.mode == 3 || settings.mode == 1) {
        runtime.inputPhysical_global = scale * 10;
    } else if(settings.mode == 4) {
        runtime.inputPhysical_global = scale * 20;
    }
    
    if(settings.mode == 4) {
        runtime.inputPhysical_global *= Constants::MA_SCALE_FACTOR_4_20MA;
    } else if(settings.mode == 1) {
        runtime.inputPhysical_global *= Constants::RESISTANCE_SCALE_FACTOR;
    } else {
        runtime.inputPhysical_global *= Constants::VOLTAGE_SCALE_FACTOR;
    }
    
    return constrain(applyBreakpoints(runtime.inputPhysical_global), 
                     Constants::I_MIN, Constants::I_MAX);
}

float computeCurrentFeedback(float Vfb_mV) {
    float current = (Vfb_mV - Constants::FB_OFFSET) * Constants::FB_TO_CURRENT_FACTOR;
    return constrain(current, 0.0f, Constants::I_MAX);
}

float updatePIDController(float Icmd, float Iout) {
    // Guard against NaN/Inf from breakpoint calculations
    if(isnan(Icmd) || isinf(Icmd)) Icmd = 0.0f;
    if(isnan(Iout) || isinf(Iout)) Iout = 0.0f;

    float error = Icmd - Iout;
    
    if(runtime.duty_p > Constants::DUTY_MIN && runtime.duty_p < Constants::DUTY_MAX) {
        runtime.integral += error;
    }
    runtime.integral = constrain(runtime.integral, -Constants::INT_LIMIT, Constants::INT_LIMIT);
    
    float duty_change = (settings.Kp * error + settings.Ki * runtime.integral) / Constants::I_MAX;
    runtime.duty_p += duty_change;
    runtime.duty_p = constrain(runtime.duty_p, Constants::DUTY_MIN, Constants::DUTY_MAX);
    
    float dither = computeDither();
    float final_duty = runtime.duty_p + dither;
    
    return constrain(final_duty, Constants::DUTY_MIN, Constants::DUTY_MAX);
}

void updateControlLoop() {
    runtime.Vcmd_raw = readADS1115Voltage(0, runtime.Vcmd_adc);
    runtime.Vfb_raw = readADS1115Voltage(1, runtime.Vfb_adc);
    
    float Vcmd_mV = runtime.Vcmd_raw * 1000.0;
    float Vfb_mV = runtime.Vfb_raw * 1000.0;
    
    runtime.Vcmd_f = lowPassFilter(runtime.Vcmd_f, Vcmd_mV);
    runtime.Vfb_f = lowPassFilter(runtime.Vfb_f, Vfb_mV);
    
    runtime.Vcmd_f_v = runtime.Vcmd_f / 1000.0;
    runtime.Vfb_f_v = runtime.Vfb_f / 1000.0;
    
    float commandInputMv = runtime.Vcmd_f;
    if (isUpdateSmoothActive()) {
        // Safety behavior during update: ignore live knob/input and use virtual 0V.
        commandInputMv = 0.0f;
    }

    float Icmd_target = computeCommandCurrent(commandInputMv);
    float Icmd = applyCommandSlew(Icmd_target);
    float Iout = computeCurrentFeedback(runtime.Vfb_f);
    float duty_cycle = updatePIDController(Icmd, Iout);
    
    setPWMDutyCycle(duty_cycle);
    
    digitalWrite(Pins::SAT_LED, runtime.inputPhysical_global >= settings.scale_in_max);
    
    runtime.Icmd_target = Icmd_target;
    runtime.Icmd = Icmd;
    runtime.Iout = Iout;
    runtime.duty_cycle = duty_cycle;
}

void processSerial() {
    while(Serial.available()) {
        char inChar = (char)Serial.read();
        if (DebugConfig::SERIAL_TRAFFIC) {
            Serial.print("USB RX CHAR: ");
            Serial.println((int)inChar);
        }

        if(inChar == '\r') {
            continue;
        }

        if(inChar == '\n') {
            runtime.inputString.trim();
            if (DebugConfig::SERIAL_TRAFFIC) {
                Serial.print("USB FULL CMD: ");
                Serial.println(runtime.inputString);
            }

            if(runtime.inputString.length() > 0) {
                String cmd = runtime.inputString;
                cmd.trim();

                if (processUARTSessionControlCommand(cmd, true)) {
                    runtime.inputString = "";
                    continue;
                }

                if (deviceConnected) {
                    Serial.println("uart_blocked_ble_active");
                    runtime.inputString = "";
                    continue;
                }

                markUARTActivity();

                // GUI compatibility when connected on USB Serial
                if(cmd.equalsIgnoreCase("read")) {
                    Serial.println(buildTelemetryPayload());
                }
                else if(cmd.startsWith("auth:")) {
                    String password = cmd.substring(5);
                    password.trim();
                    if (password == AUTH_PASSWORD) {
                        uartAuthenticated = true;
                        Serial.println("auth_success");
                    } else {
                        uartAuthenticated = false;
                        Serial.println("auth_failed");
                    }
                }
                else if(cmd.startsWith("update:")) {
                    if (!uartAuthenticated) {
                        Serial.println("auth_required");
                        runtime.inputString = "";
                        continue;
                    }
                    // One-time authorization per update action.
                    uartAuthenticated = false;

                    int colonPos = cmd.indexOf(':');
                    if (colonPos == -1) {
                        Serial.println("update_failed");
                    } else {
                        String payload = cmd.substring(colonPos + 1);
                        payload.trim();

                        if (payload.startsWith("CFG:")) {
                            String json = payload.substring(4);
                            lastConfig = "CFG:" + json;
                            pendingConfigSave = true;
                            parseAndApplyConfig(json);

                            runtime.integral = 0;
                            runtime.duty_p = constrain(runtime.duty_p, Constants::DUTY_MIN, Constants::DUTY_MAX);
                            Serial.println("device updated");
                        }
                        else if (payload.startsWith("ADV:")) {
                            String json = payload.substring(4);
                            lastAdvanced = "ADV:" + json;
                            pendingAdvancedSave = true;
                            parseAndApplyAdvanced(json);
                            Serial.println("device updated");
                        }
                        else {
                            Serial.println("update_failed");
                        }
                    }
                }
                else if(cmd.startsWith("CFG:") || cmd.startsWith("ADV:")) {
                    if (!uartAuthenticated) {
                        Serial.println("auth_required");
                        runtime.inputString = "";
                        continue;
                    }
                    uartAuthenticated = false;
                    handleUpdate("update:" + cmd);
                }
                else if(cmd.equalsIgnoreCase("version")) {
                    Serial.print("FW:");
                    Serial.println(FW_VERSION);
                }
                else if(cmd.equalsIgnoreCase("devname")) {
                    String dn = (deviceName.length() > 0) ? deviceName : "SERVO_EVDR";
                    Serial.print("DEVNAME:");
                    Serial.println(dn);
                }
                else if(cmd.equalsIgnoreCase("readcfg")) {
                    Serial.println(lastConfig);
                }
                else if(cmd.equalsIgnoreCase("readadv")) {
                    Serial.println(lastAdvanced);
                }
                else if(cmd.startsWith("KP:")) {
                    float val = cmd.substring(3).toFloat();
                    if(val >= 0 && val <= 1) {
                        settings.Kp = val;
                        pendingConfigSave = true;
                        Serial.print("✓ KP = ");
                        Serial.println(settings.Kp, 6);
                    } else {
                        Serial.println("ERROR: KP must be 0-1");
                    }
                }
                else if(cmd.startsWith("KI:")) {
                    float val = cmd.substring(3).toFloat();
                    if(val >= 0 && val <= 0.01) {
                        settings.Ki = val;
                        pendingConfigSave = true;
                        Serial.print("✓ KI = ");
                        Serial.println(settings.Ki, 8);
                    } else {
                        Serial.println("ERROR: KI must be 0-0.01");
                    }
                }
                else if(cmd.startsWith("DF:")) {
                    float val = cmd.substring(3).toFloat();
                    if(val >= 10 && val <= 500) {
                        settings.dither_freq = val;
                        pendingAdvancedSave = true;
                        resetDitherState();
                        Serial.print("✓ Dither Freq = ");
                        Serial.print(settings.dither_freq);
                        Serial.println(" Hz");
                    } else {
                        Serial.println("ERROR: Freq must be 10-500 Hz");
                    }
                }
                else if(cmd.startsWith("DA:")) {
                    float val = cmd.substring(3).toFloat();
                    if(val >= 0 && val <= 0.2) {
                        settings.dither_ampl = val;
                        pendingAdvancedSave = true;
                        resetDitherState();
                        Serial.print("✓ Dither Ampl = ");
                        Serial.print(settings.dither_ampl * 100);
                        Serial.println("%");
                    } else {
                        Serial.println("ERROR: Ampl must be 0-0.2");
                    }
                }
                else if(cmd.equalsIgnoreCase("DITHER ON")) {
                    settings.dither_enable = true;
                    resetDitherState();
                    pendingAdvancedSave = true;
                    Serial.println("✓ Dither ENABLED");
                }
                else if(cmd.equalsIgnoreCase("DITHER OFF")) {
                    settings.dither_enable = false;
                    resetDitherState();
                    pendingAdvancedSave = true;
                    Serial.println("✓ Dither DISABLED");
                }
                else if(cmd.equalsIgnoreCase("GET")) {
                    Serial.println("\n=== CURRENT SETTINGS ===");
                    Serial.print("Mode: ");
                    Serial.println(settings.mode);
                    Serial.print("Input Range: ");
                    Serial.print(settings.scale_in_min);
                    Serial.print(" - ");
                    Serial.println(settings.scale_in_max);
                    Serial.print("Output Range: ");
                    Serial.print(settings.scale_out_min);
                    Serial.print(" - ");
                    Serial.println(settings.scale_out_max);
                    Serial.print("Breakpoints: ");
                    Serial.println(settings.bp_count);
                    for(int i = 0; i < settings.bp_count; i++) {
                        Serial.print("  BP");
                        Serial.print(i+1);
                        Serial.print(": ");
                        Serial.print(settings.bp_in[i]);
                        Serial.print(" -> ");
                        Serial.println(settings.bp_out[i]);
                    }
                    Serial.print("Kp=");
                    Serial.print(settings.Kp, 6);
                    Serial.print(" Ki=");
                    Serial.println(settings.Ki, 8);
                    Serial.print("Dither: ");
                    Serial.print(settings.dither_enable ? "ON" : "OFF");
                    Serial.print(" | Freq: ");
                    Serial.print(settings.dither_freq);
                    Serial.print(" Hz | Ampl: ");
                    Serial.print(settings.dither_ampl * 100);
                    Serial.println("%");
                    Serial.println();
                }
                else if(cmd.equalsIgnoreCase("RESET")) {
                    Serial.println("\nResetting to defaults...");
                    settings.magic_number = 0;
                    preferences.begin("settings", false);
                    preferences.clear();
                    preferences.end();
                    delay(1000);
                    ESP.restart();
                }
                else {
                    // Recovery path for USB serial too: salvage embedded CFG:/ADV: payload.
                    String upper = cmd;
                    upper.toUpperCase();
                    int cfgPos = upper.indexOf("CFG:");
                    int advPos = upper.indexOf("ADV:");
                    int pos = -1;
                    if (cfgPos >= 0 && advPos >= 0) pos = (cfgPos < advPos) ? cfgPos : advPos;
                    else if (cfgPos >= 0) pos = cfgPos;
                    else if (advPos >= 0) pos = advPos;

                    if (pos >= 0) {
                        String recovered = cmd.substring(pos);
                        recovered.trim();
                        if (!uartAuthenticated) {
                            Serial.println("auth_required");
                        } else {
                            uartAuthenticated = false;
                            handleUpdate("update:" + recovered);
                        }
                        runtime.inputString = "";
                        continue;
                    }

                    if(cmd.length() > 0) {
                        Serial.println("Unknown command. Available: read, auth:<password>, update:CFG:{...}, update:ADV:{...}, CFG:{...}, ADV:{...}, devname, version, readcfg, readadv, uart_connected, uart_disconnected, KP:val, KI:val, DF:val, DA:val, DITHER ON/OFF, GET, RESET");
                    }
                }
                runtime.inputString = "";
            }
        } else {
            runtime.inputString += inChar;
        }
    }
}

void printStatus() {
    if(millis() - runtime.lastPrint > 200) {
        runtime.lastPrint = millis();
        
        Serial.print("ADCcmd:");
        Serial.print(runtime.Vcmd_adc);
        Serial.print(" | ADCfb:");
        Serial.print(runtime.Vfb_adc);
        Serial.print(" | ");
        
        Serial.print("Vcmd_raw:");
        Serial.print(runtime.Vcmd_raw, 3);
        Serial.print("V | Vfb_raw:");
        Serial.print(runtime.Vfb_raw, 3);
        Serial.print("V | ");
        
        Serial.print("Vcmd_filt:");
        Serial.print(runtime.Vcmd_f_v, 3);
        Serial.print("V | Vfb_filt:");
        Serial.print(runtime.Vfb_f_v, 3);
        Serial.print("V | ");
        
        Serial.print("Input:");
        Serial.print(runtime.inputPhysical_global, 3);
        Serial.print(getInputUnit(settings.mode));
        Serial.print(" | ");
        
        Serial.print("Icmd:");
        Serial.print(runtime.Icmd, 0);
        Serial.print("mA | Iout:");
        Serial.print(runtime.Iout, 0);
        Serial.print("mA | ");
        
        Serial.print("Duty:");
        Serial.print(runtime.duty_cycle * 100, 1);
        Serial.print("%");
        
        if(settings.dither_enable) {
            Serial.print(" | Dither:");
            Serial.print(runtime.current_dither * 100, 2);
            Serial.print("%");
        }
        Serial.println();
    }
}

/* ============================================
   SETUP & LOOP
   ============================================ */
void setup() {
    Serial.begin(115200);
    delay(100);
    
    Serial.println("\n=========================================");
    Serial.println("   SERVO Device BLE Current Controller");
    Serial.println("=========================================\n");
    
    loadParametersFromEEPROM();
    
    pinMode(Pins::MCU_D2, OUTPUT);
    pinMode(Pins::MCU_D1, OUTPUT);
    pinMode(Pins::MCU_D0, OUTPUT);
    pinMode(Pins::SAT_LED, OUTPUT);
    pinMode(Pins::STATUS_LED, OUTPUT);
    digitalWrite(Pins::STATUS_LED, LOW);
    
    Wire.begin(Pins::I2C_SDA, Pins::I2C_SCL);
    Wire.setClock(400000);
    
    if (!ads.begin(0x48)) {
        Serial.println("ERROR: ADS1115 not found!");
        while(1) {
            delay(1000);
            Serial.println("Waiting for ADS1115...");
            blinkLED(3, 100);
        }
    }
    
    ads.setGain(GAIN_ONE);
    ads.setDataRate(RATE_ADS1115_860SPS);
    Serial.println("✓ ADS1115 initialized");
    
    setupPWM();
    Serial.printf("✓ PWM initialized: %d Hz on pin %d\n", PWMConfig::PWM_FREQ, Pins::PWM_PIN);
    
    loadSettings();
    
    runtime.integral = 0;
    runtime.duty_p = 0;
    resetDitherState();
    runtime.Icmd_target = 0;
    runtime.Icmd = 0;
    runtime.Icmd_slew = 0;
    runtime.lastIcmdSlewUpdateUs = 0;
    runtime.inputString = "";
    runtime.temperature = 25.0;
    runtime.supplyVoltage = 24.0;
    runtime.faultStatus = false;
    
    setupBLE();
    setupUART();
    updateBLEAvailability();
    
    Serial.println("\n=== SYSTEM ARMED ===");
    Serial.print("FW Version: ");
    Serial.println(FW_VERSION);
    Serial.print("BLE Auth Password: ");
    Serial.println(AUTH_PASSWORD);
    Serial.println("\nWaiting for BLE or UART commands...\n");
    
    blinkLED(2, 100);
}

void loop() {
    processSerial();
    processUART();
    updateBLEAvailability();
    updateControlLoop();
    if (DebugConfig::STATUS_STREAM) {
        printStatus();
    }
    
    if (pendingConfigSave) {
        pendingConfigSave = false;
        saveConfigToEEPROM();
        saveSettings();
    }
    
    if (pendingAdvancedSave) {
        pendingAdvancedSave = false;
        saveAdvancedToEEPROM();
        saveSettings();
    }
    
    if (bleAdvertisingEnabled && !deviceConnected && oldDeviceConnected) {
        delay(300);
        BLEDevice::startAdvertising();
        oldDeviceConnected = deviceConnected;
        Serial.println("BLE Advertising Restarted");
    }
    
    if (deviceConnected && !oldDeviceConnected) {
        oldDeviceConnected = deviceConnected;
    }
    
    if (bleAdvertisingEnabled && deviceConnected && (millis() - lastBLENotify > 1000)) {
        lastBLENotify = millis();
        sendBLETelemetry();
    }
    
    delay(10);
}
