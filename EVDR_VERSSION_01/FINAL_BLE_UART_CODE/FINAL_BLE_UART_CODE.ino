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
#include <Update.h>
#include <esp_ota_ops.h>
#include <esp_partition.h>
#include <freertos/FreeRTOS.h>
#include <freertos/portmacro.h>
#include <mbedtls/sha256.h>

#define _USE_MATH_DEFINES
#include <math.h>
#include <string.h>
#include <stdlib.h>

/* ============================================
   BLE CONFIGURATION
   ============================================ */
#define SERVICE_UUID     "0000ffe0-0000-1000-8000-00805f9b34fb"
#define LIVE_CHAR_UUID   "0000ffe4-0000-1000-8000-00805f9b34fb"
#define CONFIG_CHAR_UUID "0000ffe5-0000-1000-8000-00805f9b34fb"
#define ADV_CHAR_UUID    "0000ffe6-0000-1000-8000-00805f9b34fb"
#define AUTH_PASSWORD    "SERVO_EVDR_2026"
#define DEV_PASSWORD     "SERVO_DEV_2026"
#define FW_VERSION       "FINAL_BLE_UART_CODE_v1.9_ble_ota_fast"
// OTA acceptance policy for this firmware build.
static const char *const FW_ALLOWED_VARIANT = "SERVO_EVDR";
// BIN must include exact model and project markers in its image bytes.
static const char *const FW_REQUIRED_MODEL_MARKER = "FW_MODEL=SERVO_EVDR";
static const char *const FW_REQUIRED_PROJECT_MARKER = "FW_PROJECT=FINAL_BLE_UART_CODE";
// We exchange only a short SHA-256 prefix in FW_BEGIN to keep command size compact.
static const size_t FW_HASH_PREFIX_LEN = 16;
// Keep this string in the binary so desktop app can validate image identity before upload.
__attribute__((used)) static const char FW_IMAGE_IDENTITY_BLOCK[] =
    "FW_META|FW_MODEL=SERVO_EVDR|FW_PROJECT=FINAL_BLE_UART_CODE|";

/* ============================================
   PIN CONFIGURATION
   ============================================ */
namespace Pins {
    // ESP-WROOM-32 safe GPIO map (avoid flash pins 6-11 and USB serial pins 1/3).
    const uint8_t I2C_SDA = 21;
    const uint8_t I2C_SCL = 22;
    const uint8_t PWM_PIN = 25;
    const uint8_t MCU_D2 = 26;
    const uint8_t MCU_D1 = 27;
    const uint8_t MCU_D0 = 32;
    const uint8_t SAT_LED = 2;
    const uint8_t STATUS_LED = 2;
}

/* ============================================
   UART CABLE CONFIGURATION (HALF-DUPLEX)
   ============================================ */
namespace UARTConfig {
    const uint8_t UART_PORT = 2;
    const uint32_t BAUD = 9600;
    const uint8_t RX_PIN = 16;
    const uint8_t TX_PIN = 17;
    const unsigned long LOOPBACK_IGNORE_MS = 80;
    const size_t MAX_CMD_LEN = 768; // Allow full JSON update payloads over UART.
}


namespace DebugConfig {
    // Keep verbose traffic logs OFF in production; they can disturb long UART command handling.
    const bool SERIAL_TRAFFIC = false;
    const bool STATUS_STREAM = false;
}

namespace DeveloperConfig {
    const unsigned long SESSION_TIMEOUT_MS = 10UL * 60UL * 1000UL;
    const unsigned long FW_TRANSFER_TIMEOUT_MS = 20000UL;
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

struct CalibrationData {
    float ma_scale_factor_4_20ma;
    float resistance_scale_factor;
    float voltage_scale_factor;
    float fb_to_current_factor;
    float fb_offset;
    uint32_t magic_number;
};

enum DeviceState : uint8_t {
    NORMAL_MODE = 0,
    DEV_MODE_UNLOCKED = 1,
    FW_UPDATE_MODE = 2,
    RETURN_TO_NORMAL = 3
};

enum DevCommandSource : uint8_t {
    DEV_CMD_SRC_BLE = 0,
    DEV_CMD_SRC_UART = 1,
    DEV_CMD_SRC_USB = 2
};

Preferences preferences;
Configuration settings;
const uint32_t MAGIC_ID = 0xDEADBEEF;
const uint32_t CAL_MAGIC_ID = 0xC01A2026;
const uint32_t OTA_META_MAGIC_ID = 0x0A7A2026;

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
bool developerAuthenticated = false;
DeviceState deviceState = NORMAL_MODE;
bool firmwareModeActive = false;
bool firmwareTransferActive = false;
size_t fwExpectedSizeBytes = 0;
size_t fwReceivedSizeBytes = 0;
unsigned long fwLastChunkMs = 0;
bool fwRestartPending = false;
unsigned long fwRestartAtMs = 0;
unsigned long developerLastActivityMs = 0;
unsigned long lastBLENotify = 0;
bool pendingConfigSave = false;
bool pendingAdvancedSave = false;
bool bleAdvertisingEnabled = true;
String otaLastResult = "none";
mbedtls_sha256_context fwUploadShaCtx;
bool fwUploadShaActive = false;
char fwExpectedHashPrefix[FW_HASH_PREFIX_LEN + 1] = {0};
char pendingBleFirmwareCommand[UARTConfig::MAX_CMD_LEN + 1] = {0};
volatile bool pendingBleFirmwareCommandReady = false;
portMUX_TYPE pendingBleFirmwareCommandMux = portMUX_INITIALIZER_UNLOCKED;

CalibrationData calibration = {
    .ma_scale_factor_4_20ma = 1.022f,
    .resistance_scale_factor = 1.04f,
    .voltage_scale_factor = 0.9947f,
    .fb_to_current_factor = 1.630f,
    .fb_offset = 1361.0f,
    .magic_number = CAL_MAGIC_ID
};

struct OtaMetaData {
    uint32_t magic_number;
    uint8_t pending_verify;
    char expected_partition[16];
    char requested_version[32];
    char previous_version[32];
    char last_result[24];
};

OtaMetaData otaMeta = {0};

/* ============================================
   UART GLOBAL VARIABLES
   ============================================ */
HardwareSerial cableUART(UARTConfig::UART_PORT);
String rxBuffer = "";
bool ignoreRx = false;
unsigned long ignoreUntil = 0;
bool uartSessionForced = false;
bool uartSessionActive = false;
unsigned long lastUARTActivity = 0;
constexpr unsigned long UART_SESSION_IDLE_MS = 6000;
constexpr unsigned long UART_SESSION_FORCED_TIMEOUT_MS = 12000;
unsigned long uartSessionForcedStartedAt = 0;
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
String executeUpdateCommand(const String &cmd);
bool writeFirmwareChunkHex(const String &hexPayload, String &error);
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
void lockDeveloperSession();
void touchDeveloperSession();
bool isDeveloperSessionActive();
void enterFirmwareMode();
void exitFirmwareMode(bool completed, const String &reason = "none");
bool handleDeveloperControlCommand(const String &cmd, String &response, DevCommandSource source);
bool queueBleFirmwareCommand(const String &cmd);
void processPendingBleFirmwareCommand();
void saveCalibrationToNVS();
void loadCalibrationFromNVS();
void saveOtaMetaToNVS();
void loadOtaMetaFromNVS();
void finalizeOtaBootStatus();
String getDeviceStateString();
void setDeviceState(DeviceState nextState);
bool isFirmwarePriorityMode();
String detectDeviceVariant();
bool variantMatches(const String &expected, const String &actual);
void resetFirmwareHashState();
bool beginFirmwareHash(const String &expectedPrefix);
bool updateFirmwareHash(const uint8_t *data, size_t len);
bool finishFirmwareHash(String &computedPrefix);
bool isFixedLengthHex(const String &text, size_t expectedLen);

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

void saveCalibrationToNVS() {
    calibration.magic_number = CAL_MAGIC_ID;
    preferences.begin("dev_params", false);
    preferences.putBytes("calib", &calibration, sizeof(CalibrationData));
    preferences.end();
    Serial.println("Calibration saved to NVS");
}

void loadCalibrationFromNVS() {
    calibration.ma_scale_factor_4_20ma = 1.022f;
    calibration.resistance_scale_factor = 1.04f;
    calibration.voltage_scale_factor = 0.9947f;
    calibration.fb_to_current_factor = 1.630f;
    calibration.fb_offset = 1361.0f;
    calibration.magic_number = CAL_MAGIC_ID;

    preferences.begin("dev_params", true);
    const size_t sz = preferences.getBytesLength("calib");
    if (sz == sizeof(CalibrationData)) {
        CalibrationData loaded = calibration;
        preferences.getBytes("calib", &loaded, sizeof(CalibrationData));
        if (loaded.magic_number == CAL_MAGIC_ID) {
            calibration = loaded;
        }
    }
    preferences.end();
}

void saveOtaMetaToNVS() {
    otaMeta.magic_number = OTA_META_MAGIC_ID;
    preferences.begin("dev_params", false);
    preferences.putBytes("otameta", &otaMeta, sizeof(OtaMetaData));
    preferences.end();
}

void loadOtaMetaFromNVS() {
    memset(&otaMeta, 0, sizeof(otaMeta));
    preferences.begin("dev_params", true);
    const size_t sz = preferences.getBytesLength("otameta");
    if (sz == sizeof(OtaMetaData)) {
        preferences.getBytes("otameta", &otaMeta, sizeof(OtaMetaData));
        if (otaMeta.magic_number != OTA_META_MAGIC_ID) {
            memset(&otaMeta, 0, sizeof(otaMeta));
        }
    }
    preferences.end();
}

String getDeviceStateString() {
    switch (deviceState) {
        case NORMAL_MODE: return "NORMAL_MODE";
        case DEV_MODE_UNLOCKED: return "DEV_MODE_UNLOCKED";
        case FW_UPDATE_MODE: return "FW_UPDATE_MODE";
        case RETURN_TO_NORMAL: return "RETURN_TO_NORMAL";
        default: return "UNKNOWN";
    }
}

void setDeviceState(DeviceState nextState) {
    deviceState = nextState;
    if (nextState == RETURN_TO_NORMAL) {
        firmwareModeActive = false;
        firmwareTransferActive = false;
        fwExpectedSizeBytes = 0;
        fwReceivedSizeBytes = 0;
        fwLastChunkMs = 0;
        fwRestartPending = false;
        deviceState = developerAuthenticated ? DEV_MODE_UNLOCKED : NORMAL_MODE;
    }
}

bool isFirmwarePriorityMode() {
    return deviceState == FW_UPDATE_MODE;
}

void lockDeveloperSession() {
    developerAuthenticated = false;
    developerLastActivityMs = 0;
    if (firmwareTransferActive) {
        Update.abort();
    }
    firmwareTransferActive = false;
    fwExpectedSizeBytes = 0;
    fwReceivedSizeBytes = 0;
    fwLastChunkMs = 0;
    firmwareModeActive = false;
    fwRestartPending = false;
    resetFirmwareHashState();
    setDeviceState(NORMAL_MODE);
}

void touchDeveloperSession() {
    developerLastActivityMs = millis();
}

bool isDeveloperSessionActive() {
    if (!developerAuthenticated) {
        return false;
    }
    if ((millis() - developerLastActivityMs) > DeveloperConfig::SESSION_TIMEOUT_MS) {
        Serial.println("Developer session timeout -> locked");
        if (isFirmwarePriorityMode()) {
            Update.abort();
            otaLastResult = "failed_timeout";
            strncpy(otaMeta.last_result, otaLastResult.c_str(), sizeof(otaMeta.last_result) - 1);
            otaMeta.last_result[sizeof(otaMeta.last_result) - 1] = '\0';
            otaMeta.pending_verify = 0;
            saveOtaMetaToNVS();
        }
        lockDeveloperSession();
        return false;
    }
    return true;
}

void enterFirmwareMode() {
    firmwareModeActive = true;
    firmwareTransferActive = false;
    fwExpectedSizeBytes = 0;
    fwReceivedSizeBytes = 0;
    fwLastChunkMs = 0;
    fwRestartPending = false;
    resetFirmwareHashState();
    setDeviceState(FW_UPDATE_MODE);
    touchDeveloperSession();
    Serial.println("FW mode enabled: normal commands paused");
}

void exitFirmwareMode(bool completed, const String &reason) {
    if (firmwareTransferActive) {
        Update.abort();
    }
    firmwareTransferActive = false;
    fwExpectedSizeBytes = 0;
    fwReceivedSizeBytes = 0;
    fwLastChunkMs = 0;
    fwRestartPending = false;
    firmwareModeActive = false;
    resetFirmwareHashState();
    if (reason.length() > 0 && reason != "none") {
        otaLastResult = reason;
        strncpy(otaMeta.last_result, otaLastResult.c_str(), sizeof(otaMeta.last_result) - 1);
        otaMeta.last_result[sizeof(otaMeta.last_result) - 1] = '\0';
        saveOtaMetaToNVS();
    }
    setDeviceState(RETURN_TO_NORMAL);
    touchDeveloperSession();
    if (completed) {
        Serial.println("FW mode disabled: normal commands resumed");
    } else {
        Serial.println("FW mode cancelled: normal commands resumed");
    }
}

void finalizeOtaBootStatus() {
    loadOtaMetaFromNVS();

    const esp_partition_t *running = esp_ota_get_running_partition();
    if (running != nullptr) {
#if defined(ESP_OTA_IMG_PENDING_VERIFY)
        esp_ota_img_states_t otaState;
        if (esp_ota_get_state_partition(running, &otaState) == ESP_OK &&
            otaState == ESP_OTA_IMG_PENDING_VERIFY) {
            esp_ota_mark_app_valid_cancel_rollback();
        }
#endif
    }

    if (otaMeta.magic_number != OTA_META_MAGIC_ID) {
        memset(&otaMeta, 0, sizeof(otaMeta));
        otaMeta.magic_number = OTA_META_MAGIC_ID;
        strncpy(otaMeta.last_result, "none", sizeof(otaMeta.last_result) - 1);
        saveOtaMetaToNVS();
        otaLastResult = "none";
        return;
    }

    if (otaMeta.pending_verify != 0U) {
        const bool partitionMatch = (running != nullptr) &&
            (strncmp(running->label, otaMeta.expected_partition, sizeof(otaMeta.expected_partition)) == 0);
        otaLastResult = partitionMatch ? "success" : "rolled_back";
        otaMeta.pending_verify = 0;
        strncpy(otaMeta.last_result, otaLastResult.c_str(), sizeof(otaMeta.last_result) - 1);
        otaMeta.last_result[sizeof(otaMeta.last_result) - 1] = '\0';
        saveOtaMetaToNVS();
    } else {
        otaLastResult = (otaMeta.last_result[0] != '\0') ? String(otaMeta.last_result) : "none";
    }
}

static bool parseJsonFloatByKey(const String &json, const char *key, float &valueOut) {
    String token = String("\"") + key + "\"";
    int keyPos = json.indexOf(token);
    if (keyPos < 0) {
        return false;
    }
    int colonPos = json.indexOf(':', keyPos + token.length());
    if (colonPos < 0) {
        return false;
    }
    int endPos = json.indexOf(',', colonPos + 1);
    if (endPos < 0) {
        endPos = json.indexOf('}', colonPos + 1);
    }
    if (endPos < 0) {
        return false;
    }
    String val = json.substring(colonPos + 1, endPos);
    val.trim();
    valueOut = val.toFloat();
    return true;
}

static int hexNibble(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return 10 + (c - 'a');
    if (c >= 'A' && c <= 'F') return 10 + (c - 'A');
    return -1;
}

static bool isFirmwareDeveloperCommand(const String &upper) {
    return upper == "FW_ENTER" ||
           upper == "FIRMWARE_UPDATE" ||
           upper == "FW_EXIT" ||
           upper == "FW_STATUS" ||
           upper == "FW_END" ||
           upper == "FW_ABORT" ||
           upper.startsWith("FW_BEGIN:") ||
           upper.startsWith("FW_CHUNK:");
}

String detectDeviceVariant() {
    String dn = deviceName;
    dn.trim();
    dn.toUpperCase();
    if (dn.indexOf("SERVO_EVDR_DUAL") >= 0) {
        return "SERVO_EVDR_DUAL";
    }
    if (dn.indexOf(FW_ALLOWED_VARIANT) >= 0) {
        return String(FW_ALLOWED_VARIANT);
    }
    return "";
}

bool variantMatches(const String &expected, const String &actual) {
    String e = expected;
    String a = actual;
    e.trim();
    a.trim();
    e.toUpperCase();
    a.toUpperCase();
    return (e.length() > 0) && (a.length() > 0) && (e == a);
}

bool isFixedLengthHex(const String &text, size_t expectedLen) {
    if (text.length() != expectedLen) {
        return false;
    }
    for (size_t i = 0; i < text.length(); ++i) {
        const char c = text[i];
        const bool isDigitChar = (c >= '0' && c <= '9');
        const bool isUpperHex = (c >= 'A' && c <= 'F');
        if (!isDigitChar && !isUpperHex) {
            return false;
        }
    }
    return true;
}

void resetFirmwareHashState() {
    // Clean up hash context so aborted/previous sessions do not leak state.
    if (fwUploadShaActive) {
        mbedtls_sha256_free(&fwUploadShaCtx);
    }
    fwUploadShaActive = false;
    fwExpectedHashPrefix[0] = '\0';
}

bool beginFirmwareHash(const String &expectedPrefix) {
    String prefix = expectedPrefix;
    prefix.trim();
    prefix.toUpperCase();
    if (!isFixedLengthHex(prefix, FW_HASH_PREFIX_LEN)) {
        return false;
    }

    // Start SHA-256 over incoming firmware stream.
    resetFirmwareHashState();
    mbedtls_sha256_init(&fwUploadShaCtx);
    if (mbedtls_sha256_starts(&fwUploadShaCtx, 0) != 0) {
        resetFirmwareHashState();
        return false;
    }
    fwUploadShaActive = true;
    strncpy(fwExpectedHashPrefix, prefix.c_str(), FW_HASH_PREFIX_LEN);
    fwExpectedHashPrefix[FW_HASH_PREFIX_LEN] = '\0';
    return true;
}

bool updateFirmwareHash(const uint8_t *data, size_t len) {
    if (!fwUploadShaActive || data == nullptr || len == 0U) {
        return false;
    }
    return mbedtls_sha256_update(&fwUploadShaCtx, data, len) == 0;
}

bool finishFirmwareHash(String &computedPrefix) {
    computedPrefix = "";
    if (!fwUploadShaActive) {
        return false;
    }

    uint8_t digest[32] = {0};
    if (mbedtls_sha256_finish(&fwUploadShaCtx, digest) != 0) {
        resetFirmwareHashState();
        return false;
    }

    char hexDigest[65];
    for (size_t i = 0; i < 32; ++i) {
        snprintf(&hexDigest[i * 2], 3, "%02X", digest[i]);
    }
    hexDigest[64] = '\0';
    // Compare only configured prefix length with desktop-side prefix.
    computedPrefix = String(hexDigest).substring(0, FW_HASH_PREFIX_LEN);
    resetFirmwareHashState();
    return true;
}

bool queueBleFirmwareCommand(const String &cmd) {
    if (cmd.length() > UARTConfig::MAX_CMD_LEN) {
        sendBLEConfigResponse("FW_ERROR:CMD_TOO_LONG");
        return false;
    }

    bool queued = false;
    portENTER_CRITICAL(&pendingBleFirmwareCommandMux);
    if (!pendingBleFirmwareCommandReady) {
        strncpy(pendingBleFirmwareCommand, cmd.c_str(), sizeof(pendingBleFirmwareCommand) - 1);
        pendingBleFirmwareCommand[sizeof(pendingBleFirmwareCommand) - 1] = '\0';
        pendingBleFirmwareCommandReady = true;
        queued = true;
    }
    portEXIT_CRITICAL(&pendingBleFirmwareCommandMux);

    if (!queued) {
        sendBLEConfigResponse("BUSY_FW_UPDATE");
    }
    return queued;
}

void processPendingBleFirmwareCommand() {
    char command[UARTConfig::MAX_CMD_LEN + 1] = {0};
    bool hasCommand = false;

    portENTER_CRITICAL(&pendingBleFirmwareCommandMux);
    if (pendingBleFirmwareCommandReady) {
        strncpy(command, pendingBleFirmwareCommand, sizeof(command) - 1);
        command[sizeof(command) - 1] = '\0';
        pendingBleFirmwareCommand[0] = '\0';
        pendingBleFirmwareCommandReady = false;
        hasCommand = true;
    }
    portEXIT_CRITICAL(&pendingBleFirmwareCommandMux);

    if (!hasCommand) {
        return;
    }

    String response;
    handleDeveloperControlCommand(String(command), response, DEV_CMD_SRC_BLE);
    sendBLEConfigResponse(response);
}

bool writeFirmwareChunkHex(const String &hexPayload, String &error) {
    String payload = hexPayload;
    payload.trim();
    payload.replace(" ", "");
    if (payload.length() == 0 || (payload.length() % 2) != 0) {
        error = "BAD_HEX";
        return false;
    }

    const size_t bytesLen = payload.length() / 2;
    uint8_t *buf = (uint8_t *)malloc(bytesLen);
    if (buf == nullptr) {
        error = "NO_MEM";
        return false;
    }

    for (size_t i = 0; i < bytesLen; ++i) {
        int hi = hexNibble(payload[2 * i]);
        int lo = hexNibble(payload[2 * i + 1]);
        if (hi < 0 || lo < 0) {
            free(buf);
            error = "BAD_HEX";
            return false;
        }
        buf[i] = (uint8_t)((hi << 4) | lo);
    }

    size_t written = Update.write(buf, bytesLen);
    if (written != bytesLen) {
        free(buf);
        error = String("WRITE_") + String((int)Update.getError());
        return false;
    }
    if (!updateFirmwareHash(buf, bytesLen)) {
        free(buf);
        error = "HASH_UPDATE";
        return false;
    }
    free(buf);

    fwReceivedSizeBytes += written;
    fwLastChunkMs = millis();
    touchDeveloperSession();
    return true;
}

bool handleDeveloperControlCommand(const String &cmd, String &response, DevCommandSource source) {
    String text = cmd;
    text.trim();
    if (text.length() == 0) {
        return false;
    }

    String upper = text;
    upper.toUpperCase();

    // Firmware update flow must be BLE-only. UART/USB can still use calibration/user commands.
    if (source != DEV_CMD_SRC_BLE && isFirmwareDeveloperCommand(upper)) {
        response = "FW_BLE_ONLY";
        return true;
    }

    if (upper.startsWith("DEV_LOGIN:")) {
        String password = text.substring(10);
        password.trim();
        // Accept AUTH password for developer unlock as well, so desktop flows
        // can use one password prompt for firmware operations.
        if (password == DEV_PASSWORD || password == AUTH_PASSWORD) {
            developerAuthenticated = true;
            touchDeveloperSession();
            setDeviceState(DEV_MODE_UNLOCKED);
            response = "DEV_OK";
        } else {
            lockDeveloperSession();
            response = "DEV_FAIL";
        }
        return true;
    }

    if (upper == "DEV_LOGOUT") {
        if (isFirmwarePriorityMode()) {
            exitFirmwareMode(false, "failed");
        }
        lockDeveloperSession();
        response = "DEV_LOCKED";
        return true;
    }

    if (upper == "DEV_STATUS") {
        bool active = isDeveloperSessionActive();
        response = String("DEV_STATUS:") +
                   (active ? "UNLOCKED" : "LOCKED") +
                   "|STATE:" + getDeviceStateString() +
                   "|FW_RESULT:" + otaLastResult +
                   "|FW_VERSION:" + FW_VERSION;
        return true;
    }

    if (upper == "CAL_ENTER") {
        if (!isDeveloperSessionActive()) {
            response = "DEV_REQUIRED";
            return true;
        }
        setDeviceState(DEV_MODE_UNLOCKED);
        response = "CAL_MODE_READY";
        return true;
    }

    if (upper == "CALIBRATION") {
        if (!isDeveloperSessionActive()) {
            response = "DEV_REQUIRED";
            return true;
        }
        setDeviceState(DEV_MODE_UNLOCKED);
        response = "CAL_MODE_READY";
        return true;
    }

    if (upper == "CAL_GET") {
        if (!isDeveloperSessionActive()) {
            response = "DEV_REQUIRED";
            return true;
        }
        char payload[220];
        snprintf(payload, sizeof(payload),
                 "CAL:{\"MA_SCALE_FACTOR_4_20MA\":%.6f,\"RESISTANCE_SCALE_FACTOR\":%.6f,\"VOLTAGE_SCALE_FACTOR\":%.6f,\"FB_TO_CURRENT_FACTOR\":%.6f,\"FB_OFFSET\":%.3f}",
                 calibration.ma_scale_factor_4_20ma,
                 calibration.resistance_scale_factor,
                 calibration.voltage_scale_factor,
                 calibration.fb_to_current_factor,
                 calibration.fb_offset);
        response = String(payload);
        touchDeveloperSession();
        return true;
    }

    if (upper.startsWith("CAL_SET:")) {
        if (!isDeveloperSessionActive()) {
            response = "DEV_REQUIRED";
            return true;
        }
        if (isFirmwarePriorityMode()) {
            response = "BUSY_FW_UPDATE";
            return true;
        }
        String json = text.substring(8);
        float v = 0.0f;
        bool changed = false;
        if (parseJsonFloatByKey(json, "MA_SCALE_FACTOR_4_20MA", v)) {
            calibration.ma_scale_factor_4_20ma = v;
            changed = true;
        }
        if (parseJsonFloatByKey(json, "RESISTANCE_SCALE_FACTOR", v)) {
            calibration.resistance_scale_factor = v;
            changed = true;
        }
        if (parseJsonFloatByKey(json, "VOLTAGE_SCALE_FACTOR", v)) {
            calibration.voltage_scale_factor = v;
            changed = true;
        }
        if (parseJsonFloatByKey(json, "FB_TO_CURRENT_FACTOR", v)) {
            calibration.fb_to_current_factor = v;
            changed = true;
        }
        if (parseJsonFloatByKey(json, "FB_OFFSET", v)) {
            calibration.fb_offset = v;
            changed = true;
        }

        if (!changed) {
            response = "CAL_ERROR:EMPTY";
            return true;
        }

        saveCalibrationToNVS();
        touchDeveloperSession();
        response = "CAL_SAVED";
        return true;
    }

    if (upper == "CAL_EXIT") {
        if (!isDeveloperSessionActive()) {
            response = "DEV_REQUIRED";
            return true;
        }
        setDeviceState(DEV_MODE_UNLOCKED);
        touchDeveloperSession();
        response = "CAL_MODE_OFF";
        return true;
    }

    if (upper == "FW_ENTER") {
        if (!isDeveloperSessionActive()) {
            response = "DEV_REQUIRED";
            return true;
        }
        const esp_partition_t *next = esp_ota_get_next_update_partition(nullptr);
        if (next == nullptr) {
            response = "FW_ERROR:NO_OTA_SLOT";
            return true;
        }
        if (!isFirmwarePriorityMode()) {
            enterFirmwareMode();
        }
        String target = (next != nullptr) ? String(next->label) : "none";
        response = String("FW_MODE_READY|FW:") + FW_VERSION + "|TARGET:" + target + "|LAST:" + otaLastResult +
                   "|VARIANT:" + detectDeviceVariant();
        return true;
    }

    if (upper == "FIRMWARE_UPDATE") {
        if (!isDeveloperSessionActive()) {
            response = "DEV_REQUIRED";
            return true;
        }
        const esp_partition_t *next = esp_ota_get_next_update_partition(nullptr);
        if (next == nullptr) {
            response = "FW_ERROR:NO_OTA_SLOT";
            return true;
        }
        if (!isFirmwarePriorityMode()) {
            enterFirmwareMode();
        }
        response = String("FW_MODE_READY|FW:") + FW_VERSION + "|TARGET:" + String(next->label) + "|LAST:" + otaLastResult +
                   "|VARIANT:" + detectDeviceVariant();
        return true;
    }

    if (upper == "FW_EXIT") {
        if (!isFirmwarePriorityMode()) {
            response = "FW_MODE_OFF";
            return true;
        }
        exitFirmwareMode(false, "failed");
        response = "FW_MODE_OFF";
        return true;
    }

    if (upper == "FW_STATUS") {
        response = String("FW_STATUS:") + (isFirmwarePriorityMode() ? "ON" : "OFF") +
                   "|STATE:" + getDeviceStateString() +
                   "|TRANSFER:" + (firmwareTransferActive ? "ON" : "OFF") +
                   "|RX:" + String((unsigned long)fwReceivedSizeBytes) +
                   "/" + String((unsigned long)fwExpectedSizeBytes) +
                   "|LAST:" + otaLastResult +
                   "|FW:" + FW_VERSION +
                   "|VARIANT:" + detectDeviceVariant();
        return true;
    }

    if (upper.startsWith("FW_BEGIN:")) {
        if (!isDeveloperSessionActive()) {
            response = "DEV_REQUIRED";
            return true;
        }
        if (!isFirmwarePriorityMode()) {
            response = "FW_MODE_REQUIRED";
            return true;
        }

        // FW_BEGIN format:
        // FW_BEGIN:<size>,<requestedVersion>,<requestedVariant>,<sha256Prefix16>
        String args = text.substring(9);
        args.trim();
        String sizeText = args;
        String requestedVersion = "";
        String requestedVariant = "";
        String requestedHashPrefix = "";
        int comma = args.indexOf(',');
        if (comma >= 0) {
            sizeText = args.substring(0, comma);
            String tail = args.substring(comma + 1);
            tail.trim();
            int comma2 = tail.indexOf(',');
            if (comma2 >= 0) {
                requestedVersion = tail.substring(0, comma2);
                String tail2 = tail.substring(comma2 + 1);
                tail2.trim();
                int comma3 = tail2.indexOf(',');
                if (comma3 >= 0) {
                    requestedVariant = tail2.substring(0, comma3);
                    requestedHashPrefix = tail2.substring(comma3 + 1);
                } else {
                    requestedVariant = tail2;
                }
            } else {
                requestedVersion = tail;
            }
        }
        sizeText.trim();
        requestedVersion.trim();
        requestedVariant.trim();
        requestedHashPrefix.trim();
        requestedVariant.toUpperCase();
        requestedHashPrefix.toUpperCase();

        size_t expected = (size_t)strtoul(sizeText.c_str(), nullptr, 10);
        if (expected == 0U) {
            response = "FW_ERROR:BAD_SIZE";
            return true;
        }

        String deviceVariant = detectDeviceVariant();
        if (requestedVariant.length() == 0) {
            response = "FW_ERROR:VARIANT_REQUIRED";
            exitFirmwareMode(false, "failed_variant");
            return true;
        }
        if (requestedHashPrefix.length() == 0) {
            response = "FW_ERROR:HASH_REQUIRED";
            exitFirmwareMode(false, "failed_hash");
            return true;
        }
        // Prefix must be fixed-width uppercase hex.
        if (!isFixedLengthHex(requestedHashPrefix, FW_HASH_PREFIX_LEN)) {
            response = "FW_ERROR:BAD_HASH_PREFIX";
            exitFirmwareMode(false, "failed_hash");
            return true;
        }
        if (requestedVariant != String(FW_ALLOWED_VARIANT)) {
            response = String("FW_ERROR:UNSUPPORTED_BIN_VARIANT allowed=") +
                       String(FW_ALLOWED_VARIANT) +
                       " bin=" + requestedVariant;
            exitFirmwareMode(false, "failed_variant");
            return true;
        }
        if (deviceVariant.length() == 0) {
            response = "FW_ERROR:DEVICE_VARIANT_UNKNOWN";
            exitFirmwareMode(false, "failed_variant");
            return true;
        }
        if (deviceVariant != String(FW_ALLOWED_VARIANT)) {
            String dev = deviceVariant;
            dev.trim();
            dev.toUpperCase();
            response = String("FW_ERROR:UNSUPPORTED_DEVICE_VARIANT allowed=") +
                       String(FW_ALLOWED_VARIANT) +
                       " dev=" + dev;
            exitFirmwareMode(false, "failed_variant");
            return true;
        }
        if (!variantMatches(requestedVariant, deviceVariant)) {
            String req = requestedVariant;
            req.trim();
            req.toUpperCase();
            String dev = deviceVariant;
            dev.trim();
            dev.toUpperCase();
            response = String("FW_ERROR:VARIANT_MISMATCH dev=") + dev + " bin=" + req;
            exitFirmwareMode(false, "failed_variant");
            return true;
        }

        const esp_partition_t *next = esp_ota_get_next_update_partition(nullptr);
        if (next == nullptr) {
            response = "FW_ERROR:NO_OTA_SLOT";
            exitFirmwareMode(false, "failed_no_slot");
            return true;
        }
        if (expected > next->size) {
            response = String("FW_ERROR:SIZE_GT_SLOT ") +
                       String((unsigned long)expected) + "/" +
                       String((unsigned long)next->size);
            exitFirmwareMode(false, "failed_size");
            return true;
        }

        if (firmwareTransferActive) {
            Update.abort();
        }

        Serial.print("FW_BEGIN size=");
        Serial.print((unsigned long)expected);
        Serial.print(" target=");
        Serial.print(next->label);
        Serial.print(" slot=");
        Serial.println((unsigned long)next->size);

        if (!Update.begin(expected, U_FLASH)) {
            response = String("FW_ERROR:BEGIN_") + String((int)Update.getError());
            exitFirmwareMode(false, "failed");
            return true;
        }
        if (!beginFirmwareHash(requestedHashPrefix)) {
            Update.abort();
            response = "FW_ERROR:HASH_INIT";
            exitFirmwareMode(false, "failed_hash");
            return true;
        }

        fwExpectedSizeBytes = expected;
        fwReceivedSizeBytes = 0;
        fwLastChunkMs = millis();
        firmwareTransferActive = true;
        fwRestartPending = false;

        memset(&otaMeta, 0, sizeof(otaMeta));
        otaMeta.magic_number = OTA_META_MAGIC_ID;
        otaMeta.pending_verify = 0;
        strncpy(otaMeta.previous_version, FW_VERSION, sizeof(otaMeta.previous_version) - 1);
        if (requestedVersion.length() > 0) {
            strncpy(otaMeta.requested_version, requestedVersion.c_str(), sizeof(otaMeta.requested_version) - 1);
        } else {
            strncpy(otaMeta.requested_version, "unknown", sizeof(otaMeta.requested_version) - 1);
        }
        if (next != nullptr) {
            strncpy(otaMeta.expected_partition, next->label, sizeof(otaMeta.expected_partition) - 1);
        } else {
            strncpy(otaMeta.expected_partition, "unknown", sizeof(otaMeta.expected_partition) - 1);
        }
        strncpy(otaMeta.last_result, "in_progress", sizeof(otaMeta.last_result) - 1);
        saveOtaMetaToNVS();

        touchDeveloperSession();
        response = String("FW_READY:") + String((unsigned long)fwExpectedSizeBytes);
        return true;
    }

    if (upper.startsWith("FW_CHUNK:")) {
        if (!isDeveloperSessionActive()) {
            response = "DEV_REQUIRED";
            return true;
        }
        if (!isFirmwarePriorityMode()) {
            response = "FW_MODE_REQUIRED";
            return true;
        }
        if (!firmwareTransferActive) {
            response = "FW_ERROR:NO_BEGIN";
            return true;
        }
        String chunkPayload = text.substring(9);
        chunkPayload.trim();

        int payloadSep = chunkPayload.indexOf(':');
        if (payloadSep > 0) {
            String offsetText = chunkPayload.substring(0, payloadSep);
            offsetText.trim();
            bool numericOffset = true;
            for (size_t i = 0; i < offsetText.length(); ++i) {
                if (!isDigit(offsetText[i])) {
                    numericOffset = false;
                    break;
                }
            }

            if (numericOffset) {
                size_t chunkOffset = (size_t)strtoul(offsetText.c_str(), nullptr, 10);
                if (chunkOffset < fwReceivedSizeBytes) {
                    // Duplicate chunk after a lost ACK; report current progress without rewriting.
                    response = String("FW_CHUNK_ACK:") + String((unsigned long)fwReceivedSizeBytes);
                    fwLastChunkMs = millis();
                    touchDeveloperSession();
                    return true;
                }
                if (chunkOffset > fwReceivedSizeBytes) {
                    response = String("FW_ERROR:OFFSET ") +
                               String((unsigned long)chunkOffset) + "/" +
                               String((unsigned long)fwReceivedSizeBytes);
                    return true;
                }
                chunkPayload = chunkPayload.substring(payloadSep + 1);
                chunkPayload.trim();
            }
        }

        String error;
        if (!writeFirmwareChunkHex(chunkPayload, error)) {
            Update.abort();
            firmwareTransferActive = false;
            response = "FW_ERROR:" + error;
            exitFirmwareMode(false, "failed");
            return true;
        }
        response = String("FW_CHUNK_ACK:") + String((unsigned long)fwReceivedSizeBytes);
        return true;
    }

    if (upper == "FW_END") {
        if (!isDeveloperSessionActive()) {
            response = "DEV_REQUIRED";
            return true;
        }
        if (!isFirmwarePriorityMode()) {
            response = "FW_MODE_REQUIRED";
            return true;
        }
        if (!firmwareTransferActive) {
            response = "FW_ERROR:NO_BEGIN";
            return true;
        }
        if (fwExpectedSizeBytes > 0U && fwReceivedSizeBytes != fwExpectedSizeBytes) {
            response = String("FW_ERROR:INCOMPLETE ") +
                       String((unsigned long)fwReceivedSizeBytes) + "/" +
                       String((unsigned long)fwExpectedSizeBytes);
            return true;
        }
        // Verify integrity of received image before committing OTA slot.
        String expectedHashPrefix = String(fwExpectedHashPrefix);
        expectedHashPrefix.trim();
        expectedHashPrefix.toUpperCase();
        String computedHashPrefix;
        if (!finishFirmwareHash(computedHashPrefix)) {
            Update.abort();
            firmwareTransferActive = false;
            response = "FW_ERROR:HASH_FINALIZE";
            exitFirmwareMode(false, "failed_hash");
            return true;
        }
        if (computedHashPrefix != expectedHashPrefix) {
            Update.abort();
            firmwareTransferActive = false;
            response = String("FW_ERROR:HASH_MISMATCH expected=") +
                       expectedHashPrefix + " got=" + computedHashPrefix;
            exitFirmwareMode(false, "failed_hash");
            return true;
        }
        if (!Update.end(false)) {
            response = String("FW_ERROR:END_") + String((int)Update.getError());
            firmwareTransferActive = false;
            exitFirmwareMode(false, "failed");
            return true;
        }

        firmwareTransferActive = false;
        otaMeta.pending_verify = 1;
        strncpy(otaMeta.last_result, "pending_reboot", sizeof(otaMeta.last_result) - 1);
        saveOtaMetaToNVS();
        otaLastResult = "pending_reboot";

        fwRestartPending = true;
        fwRestartAtMs = millis() + 1200UL;
        response = String("FW_TRANSFER_DONE:") + String((unsigned long)fwReceivedSizeBytes);
        return true;
    }

    if (upper == "FW_ABORT") {
        if (firmwareTransferActive) {
            Update.abort();
        }
        firmwareTransferActive = false;
        response = "FW_ABORTED";
        exitFirmwareMode(false, "failed");
        return true;
    }

    return false;
}

void markUARTActivity() {
    lastUARTActivity = millis();
    if (uartSessionForced) {
        // Keep forced UART ownership alive while host is actively sending commands.
        uartSessionForcedStartedAt = lastUARTActivity;
    }
    if (!uartSessionActive) {
        uartSessionActive = true;
        Serial.println("UART session active -> BLE disabled");
    }
}

void setUARTSessionForced(bool active) {
    uartSessionForced = active;
    if (active) {
        uartSessionForcedStartedAt = millis();
        markUARTActivity();
    } else {
        uartSessionActive = false;
        uartSessionForcedStartedAt = 0;
        lastUARTActivity = 0;
        Serial.println("UART session released -> BLE can resume");
    }
}

bool isUARTSessionActive() {
    const unsigned long now = millis();
    if (uartSessionForced) {
        const bool keepForcedAlive =
            (uartSessionForcedStartedAt != 0 && (now - uartSessionForcedStartedAt) <= UART_SESSION_FORCED_TIMEOUT_MS) ||
            (lastUARTActivity != 0 && (now - lastUARTActivity) <= UART_SESSION_FORCED_TIMEOUT_MS);
        if (keepForcedAlive) {
            return true;
        }
        // Recovery path: if host never sends uart_disconnected, auto-release BLE after timeout.
        uartSessionForced = false;
        uartSessionActive = false;
        uartSessionForcedStartedAt = 0;
        lastUARTActivity = 0;
        Serial.println("UART forced session timeout -> BLE can resume");
        return false;
    }
    if (!uartSessionActive) {
        return false;
    }
    if ((now - lastUARTActivity) > UART_SESSION_IDLE_MS) {
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

    BLEAdvertising *pAdvertising = BLEDevice::getAdvertising();
    if (bleAdvertisingEnabled) {
        // Use framework start helper to reliably re-arm advertising after UART session release.
        BLEDevice::startAdvertising();
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
        lockDeveloperSession();
        Serial.println("✓ BLE Client Connected");
        blinkLED(2, 120);
    }

    void onDisconnect(BLEServer *server) override {
        deviceConnected = false;
        if (firmwareModeActive) {
            exitFirmwareMode(false, "failed");
        }
        isAuthenticated = false;
        lockDeveloperSession();
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

        if (rx.startsWith("FW_CHUNK:")) {
            if (DebugConfig::SERIAL_TRAFFIC) {
                Serial.print("CONFIG WRITE: FW_CHUNK:<");
                Serial.print(rx.length() - 9);
                Serial.println(" chars>");
            }
        } else {
            Serial.print("CONFIG WRITE: ");
            Serial.println(rx);
        }

        String upper = rx;
        upper.toUpperCase();
        if (isFirmwareDeveloperCommand(upper)) {
            queueBleFirmwareCommand(rx);
            return;
        }

        String devResponse;
        if (handleDeveloperControlCommand(rx, devResponse, DEV_CMD_SRC_BLE)) {
            sendBLEConfigResponse(devResponse);
            return;
        }

        if (firmwareModeActive) {
            sendBLEConfigResponse("BUSY_FW_UPDATE");
            Serial.println("BLE CONFIG blocked: firmware mode active");
            return;
        }

        if (rx.startsWith("AUTH:")) {
            String password = rx.substring(5);
            password.trim();
            if (password == AUTH_PASSWORD) {
                isAuthenticated = true;
                sendBLEConfigResponse("AUTH_SUCCESS");
            } else {
                isAuthenticated = false;
                sendBLEConfigResponse("AUTH_FAILED");
            }
            return;
        }

        // Handle GET - Load configuration from device
        if (rx.equalsIgnoreCase("GET")) {
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
        if (firmwareModeActive) {
            pCharacteristic->setValue("BUSY_FW_UPDATE");
            return;
        }
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

        if (rx.startsWith("FW_CHUNK:")) {
            if (DebugConfig::SERIAL_TRAFFIC) {
                Serial.print("ADV WRITE: FW_CHUNK:<");
                Serial.print(rx.length() - 9);
                Serial.println(" chars>");
            }
        } else {
            Serial.print("ADV WRITE: ");
            Serial.println(rx);
        }

        String upper = rx;
        upper.toUpperCase();
        if (isFirmwareDeveloperCommand(upper)) {
            queueBleFirmwareCommand(rx);
            return;
        }

        String devResponse;
        if (handleDeveloperControlCommand(rx, devResponse, DEV_CMD_SRC_BLE)) {
            sendBLEAdvResponse(devResponse);
            return;
        }

        if (firmwareModeActive) {
            sendBLEAdvResponse("BUSY_FW_UPDATE");
            Serial.println("BLE ADV blocked: firmware mode active");
            return;
        }

        // Handle GET for advanced
        if (rx.equalsIgnoreCase("GET")) {
            sendBLEAdvResponse(lastAdvanced);
            Serial.println("✓ ADVANCED SENT to client");
            return;
        }

        if (!isAuthenticated) {
            sendBLEAdvResponse("ERROR: Authentication required");
            Serial.println("ADV REJECTED: not authenticated");
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
        if (firmwareModeActive) {
            pCharacteristic->setValue("BUSY_FW_UPDATE");
            return;
        }
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
    if (!deviceConnected || isUARTSessionActive() || firmwareModeActive) return;

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
    Serial.print("User Auth Password: ");
    Serial.println(AUTH_PASSWORD);
    Serial.print("Developer Password: ");
    Serial.println(DEV_PASSWORD);
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

String executeUpdateCommand(const String &cmd) {
    if (firmwareModeActive) {
        return "BUSY_FW_UPDATE";
    }

    if (!isAuthenticated) {
        return "AUTH_REQUIRED";
    }

    int colonPos = cmd.indexOf(':');
    if (colonPos == -1) {
        return "update_failed";
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
        return "device updated";
    }
    else if (payload.startsWith("ADV:")) {
        String json = payload.substring(4);
        lastAdvanced = "ADV:" + json;
        pendingAdvancedSave = true;
        parseAndApplyAdvanced(json);
        return "device updated";
    }

    return "update_failed";
}

void handleUpdate(String cmd) {
    sendLine(executeUpdateCommand(cmd));
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

    String controlResponse;
    if (handleDeveloperControlCommand(cmd, controlResponse, DEV_CMD_SRC_UART)) {
        sendLine(controlResponse);
        return;
    }

    if (firmwareModeActive) {
        sendLine("BUSY_FW_UPDATE");
        return;
    }

    if (cmd.equals("read")) {
        sendValues();
    }
    else if (cmd.equalsIgnoreCase("devname")) {
        String dn = (deviceName.length() > 0) ? deviceName : "SERVO_EVDR";
        sendLine(String("DEVNAME:") + dn);
    }
    else if (cmd.equalsIgnoreCase("version")) {
        sendLine(String("FW:") + FW_VERSION + "|OTA_RESULT:" + otaLastResult + "|STATE:" + getDeviceStateString());
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
            isAuthenticated = true;
            sendLine("auth_success");
        } else {
            isAuthenticated = false;
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
        runtime.inputPhysical_global *= calibration.ma_scale_factor_4_20ma;
    } else if(settings.mode == 1) {
        runtime.inputPhysical_global *= calibration.resistance_scale_factor;
    } else {
        runtime.inputPhysical_global *= calibration.voltage_scale_factor;
    }
    
    return constrain(applyBreakpoints(runtime.inputPhysical_global), 
                     Constants::I_MIN, Constants::I_MAX);
}

float computeCurrentFeedback(float Vfb_mV) {
    float current = (Vfb_mV - calibration.fb_offset) * calibration.fb_to_current_factor;
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
    if (firmwareModeActive) {
        // Firmware transfer has priority; keep last applied PWM state unchanged.
        return;
    }

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

                String controlResponse;
                if (handleDeveloperControlCommand(cmd, controlResponse, DEV_CMD_SRC_USB)) {
                    Serial.println(controlResponse);
                    runtime.inputString = "";
                    continue;
                }

                if (firmwareModeActive) {
                    Serial.println("BUSY_FW_UPDATE");
                    runtime.inputString = "";
                    continue;
                }

                // GUI compatibility when connected on USB Serial
                if(cmd.equalsIgnoreCase("read")) {
                    Serial.println(buildTelemetryPayload());
                }
                else if(cmd.startsWith("auth:")) {
                    String password = cmd.substring(5);
                    password.trim();
                    if (password == AUTH_PASSWORD) {
                        isAuthenticated = true;
                        Serial.println("auth_success");
                    } else {
                        isAuthenticated = false;
                        Serial.println("auth_failed");
                    }
                }
                else if(cmd.startsWith("update:")) {
                    Serial.println(executeUpdateCommand(cmd));
                }
                else if(cmd.startsWith("CFG:") || cmd.startsWith("ADV:")) {
                    Serial.println(executeUpdateCommand("update:" + cmd));
                }
                else if(cmd.equalsIgnoreCase("version")) {
                    Serial.print("FW:");
                    Serial.print(FW_VERSION);
                    Serial.print("|OTA_RESULT:");
                    Serial.print(otaLastResult);
                    Serial.print("|STATE:");
                    Serial.println(getDeviceStateString());
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
                        Serial.println(executeUpdateCommand("update:" + recovered));
                        runtime.inputString = "";
                        continue;
                    }

                    if(cmd.length() > 0) {
                        Serial.println("Unknown command. Available: auth:<password>, dev_login:<password>, dev_logout, dev_status, cal_enter, cal_get, cal_set:{...}, cal_exit, fw_enter/fw_begin/fw_chunk/fw_end/fw_abort/fw_exit/fw_status (BLE only), read, update:CFG:{...}, update:ADV:{...}, CFG:{...}, ADV:{...}, devname, version, readcfg, readadv, uart_connected, uart_disconnected, KP:val, KI:val, DF:val, DA:val, DITHER ON/OFF, GET, RESET");
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
    loadCalibrationFromNVS();
    finalizeOtaBootStatus();
    setDeviceState(NORMAL_MODE);
    
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
    isAuthenticated = false;
    lockDeveloperSession();
    firmwareModeActive = false;
    
    setupBLE();
    setupUART();
    updateBLEAvailability();
    
    Serial.println("\n=== SYSTEM ARMED ===");
    Serial.print("FW Version: ");
    Serial.println(FW_VERSION);
    Serial.print("FW Model Marker: ");
    Serial.println(FW_REQUIRED_MODEL_MARKER);
    Serial.print("FW Project Marker: ");
    Serial.println(FW_REQUIRED_PROJECT_MARKER);
    Serial.print("OTA Last Result: ");
    Serial.println(otaLastResult);
    Serial.print("User Auth Password: ");
    Serial.println(AUTH_PASSWORD);
    Serial.print("Developer Password: ");
    Serial.println(DEV_PASSWORD);
    Serial.println("\nWaiting for BLE or UART commands...\n");
    
    blinkLED(2, 100);
}

void loop() {
    if (developerAuthenticated) {
        (void)isDeveloperSessionActive();
    }

    processPendingBleFirmwareCommand();

    if (isFirmwarePriorityMode() && firmwareTransferActive && fwLastChunkMs != 0UL) {
        if ((millis() - fwLastChunkMs) > DeveloperConfig::FW_TRANSFER_TIMEOUT_MS) {
            Update.abort();
            firmwareTransferActive = false;
            otaLastResult = "failed_timeout";
            otaMeta.pending_verify = 0;
            strncpy(otaMeta.last_result, otaLastResult.c_str(), sizeof(otaMeta.last_result) - 1);
            otaMeta.last_result[sizeof(otaMeta.last_result) - 1] = '\0';
            saveOtaMetaToNVS();
            exitFirmwareMode(false, "failed_timeout");
        }
    }

    if (fwRestartPending && millis() >= fwRestartAtMs) {
        Serial.println("FW update success. Rebooting now...");
        delay(50);
        ESP.restart();
    }

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
