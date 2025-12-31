#include <Arduino.h>

// Pin Definitions
#define TURBIDITY_PIN 34
#define PH_PIN 35
#define TDS_PIN 32
#define RELAY_PIN 26

String command = "";

void setup() {
  Serial.begin(115200); // Must match Python BAUD_RATE
  
  pinMode(RELAY_PIN, OUTPUT);
  digitalWrite(RELAY_PIN, LOW); // Start with Valve OPEN (Active Low/High depends on relay)
  
  // Analog Pins Setup
  pinMode(TURBIDITY_PIN, INPUT);
  pinMode(PH_PIN, INPUT);
  pinMode(TDS_PIN, INPUT);
}

void loop() {
  // 1. Read Sensors (Simulated math for demo, calibrate for real sensors)
  int turbValue = analogRead(TURBIDITY_PIN);
  float phValue = analogRead(PH_PIN) * (14.0 / 4095.0); // Simple map
  int tdsValue = analogRead(TDS_PIN) / 2;
  
  // Convert Turbidity to NTU (Approximate formula)
  int turbidity = map(turbValue, 0, 4095, 100, 0); 
  if (turbidity < 0) turbidity = 0;

  // 2. Send Data to Python as JSON String
  // Format: {"ph": 7.2, "turbidity": 10, "tds": 150}
  Serial.print("{\"ph\":");
  Serial.print(phValue, 1);
  Serial.print(", \"turbidity\":");
  Serial.print(turbidity);
  Serial.print(", \"tds\":");
  Serial.print(tdsValue);
  Serial.println("}");

  // 3. Check for Commands from Python
  if (Serial.available() > 0) {
    command = Serial.readStringUntil('\n');
    command.trim(); // Remove spaces
    
    if (command == "CLOSE") {
      digitalWrite(RELAY_PIN, HIGH); // Turn Relay ON (Close Valve)
    } 
    else if (command == "OPEN") {
      digitalWrite(RELAY_PIN, LOW);  // Turn Relay OFF (Open Valve)
    }
  }

  delay(1000); // Send data every 1 second
}