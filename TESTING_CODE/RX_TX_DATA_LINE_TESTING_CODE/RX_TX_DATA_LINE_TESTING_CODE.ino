HardwareSerial mySerial(0);

String rxBuffer = "";

// Stored values
float voltage = 12.5;
int current = 250;
String mode = "AUTO";

bool ignoreRx = false;
unsigned long ignoreUntil = 0;

void startIgnoreRx(unsigned long ms) {
  ignoreRx = true;
  ignoreUntil = millis() + ms;
}

void updateIgnoreRx() {
  if (ignoreRx && millis() > ignoreUntil) {
    ignoreRx = false;

    // clear anything looped back during transmit
    while (mySerial.available()) {
      mySerial.read();
    }
  }
}

void sendLine(String msg) {
  startIgnoreRx(80);   // ignore loopback briefly
  mySerial.println(msg);
}

// dummy values only for testing - replace with actual sensor readings and logic
void sendValues() {
  String data = "VOLT=" + String(voltage, 1) +
                ",CURR=" + String(current) +
                ",MODE=" + mode;
  sendLine(data);
}

void handleUpdate(String cmd) {
  int colonPos = cmd.indexOf(':');
  if (colonPos == -1) {
    sendLine("update_failed");
    return;
  }

  String payload = cmd.substring(colonPos + 1);

  int c1 = payload.indexOf(',');
  int c2 = payload.indexOf(',', c1 + 1);

  if (c1 == -1 || c2 == -1) {
    sendLine("update_failed");
    return;
  }

  String voltStr = payload.substring(0, c1);
  String currStr = payload.substring(c1 + 1, c2);
  String modeStr = payload.substring(c2 + 1);

  voltStr.trim();
  currStr.trim();
  modeStr.trim();

  voltage = voltStr.toFloat();
  current = currStr.toInt();
  mode = modeStr;

  sendLine("device updated");
}

void setup() {
  Serial.begin(115200);
  mySerial.begin(9600, SERIAL_8N1, 5, 6); // RX, TX
  delay(300);

  while (mySerial.available()) {
    mySerial.read();
  }

  Serial.println("SERVO Device Ready");
}

void loop() {
  updateIgnoreRx();

  if (ignoreRx) {
    return;
  }

  while (mySerial.available()) {
    char c = mySerial.read();

    Serial.print("RX CHAR: ");
    Serial.println((int)c);

    if (c == '\r') {
      continue;
    }

    if (c == '\n') {
      rxBuffer.trim();

      Serial.print("FULL CMD: ");
      Serial.println(rxBuffer);

      if (rxBuffer.length() > 0) {
        if (rxBuffer.equals("read")) {
          sendValues();
        }
        else if (rxBuffer.startsWith("update:")) {
          handleUpdate(rxBuffer);
        }
        else {
          sendLine("unknown_command");
        }
      }

      rxBuffer = "";
    } else {
      rxBuffer += c;

      if (rxBuffer.length() > 100) {
        rxBuffer = "";
        sendLine("buffer_cleared");
      }
    }
  }
}
