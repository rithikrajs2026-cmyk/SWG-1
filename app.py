import cv2
import serial
import time
import threading
import numpy as np
import blynklib
import os
import requests
import base64
import subprocess
import re
import json
from flask import Flask, Response, jsonify, render_template

# ================= CONFIGURATION =================
BLYNK_AUTH = "YOUR_BLYNK_AUTH_TOKEN"  
IMGBB_API_KEY = "d72547ffc90ee5788f77e6193e360c11"

# USB SERIAL SETTINGS
SERIAL_PORTS_TO_TRY = ['/dev/ttyUSB0', '/dev/ttyACM0', 'COM3', 'COM4', 'COM5']
BAUD_RATE = 9600

# SAFETY THRESHOLDS
TURBIDITY_LIMIT = 3000
PH_LOW = 6.0
PH_HIGH = 8.5
CONFIDENCE_THRESHOLD = 0.60 

# PATHS
RELAY_PIN = 17 
MODEL_PATH = "model.tflite"
LABELS_PATH = "labels.txt"

# ================= GLOBAL STATE (For Web & Blynk) =================
system_data = {
    "ph": 7.0,
    "turbidity": 0,
    "tds": 0,
    "valve_state": "OPEN",
    "status_msg": "System Safe",
    "danger_active": False,
    "evidence_url": ""
}

labels = []
interpreter = None
app = Flask(__name__)
output_frame = None
lock = threading.Lock()
public_url = None

# ================= 1. MOCK HARDWARE =================
try:
    import RPi.GPIO as GPIO
    GPIO.setmode(GPIO.BCM); GPIO.setup(RELAY_PIN, GPIO.OUT); GPIO.output(RELAY_PIN, GPIO.HIGH)
except:
    print("[SYSTEM] Running in Simulation Mode (No RPi GPIO).")
    class GPIO_MOCK:
        BCM=1; OUT=1; LOW=0; HIGH=1
        def setmode(self,m): pass
        def setup(self,p,m): pass
        def output(self,p,s): pass 
    GPIO = GPIO_MOCK()

# ================= 2. CORE FUNCTIONS =================
def upload_evidence(frame):
    try:
        _, img = cv2.imencode('.jpg', frame)
        payload = {'key': IMGBB_API_KEY, 'image': base64.b64encode(img).decode('utf-8')}
        req = requests.post('https://api.imgbb.com/1/upload', data=payload)
        if req.json()['success']: return req.json()['data']['url']
    except Exception as e: print(f"[UPLOAD ERROR] {e}")
    return None

def trigger_danger(reason, evidence_frame=None):
    global system_data
    if system_data["valve_state"] == "CLOSED": return

    # 1. Hardware Action
    GPIO.output(RELAY_PIN, GPIO.LOW)
    system_data["valve_state"] = "CLOSED"
    system_data["danger_active"] = True
    system_data["status_msg"] = f"⚠️ {reason}"
    
    print(f"\n🚨 DANGER: {reason} -> VALVE CLOSED 🚨")
    
    # 2. Blynk Alert & Evidence Upload
    def handle_alerts():
        if BLYNK_AUTH and BLYNK_AUTH != "YOUR_BLYNK_AUTH_TOKEN":
            try:
                blynk.virtual_write(4, 0) # Valve Widget
                blynk.virtual_write(0, f"⚠️ {reason}") 
                blynk.log_event("danger_alert", f"⚠️ Critical: {reason}") # Notification

                if evidence_frame is not None:
                    url = upload_evidence(evidence_frame)
                    if url: 
                        blynk.virtual_write(1, 1) # LED
                        system_data["evidence_url"] = url # For Web App
            except: pass
            
    threading.Thread(target=handle_alerts).start()
    threading.Thread(target=reset_system).start()

def reset_system():
    time.sleep(15)
    global system_data
    
    print("🔄 System Normalizing...")
    system_data["valve_state"] = "OPEN"
    system_data["danger_active"] = False
    system_data["status_msg"] = "System Safe"
    system_data["evidence_url"] = ""
    
    GPIO.output(RELAY_PIN, GPIO.HIGH)
    
    if BLYNK_AUTH and BLYNK_AUTH != "YOUR_BLYNK_AUTH_TOKEN":
        try: blynk.virtual_write(4, 1); blynk.virtual_write(0, "System Safe"); blynk.virtual_write(1, 0)
        except: pass

# ================= 3. THREADS =================
def serial_sensor_loop():
    global system_data
    ser = None
    for port in SERIAL_PORTS_TO_TRY:
        try: ser = serial.Serial(port, BAUD_RATE, timeout=1); break
        except: continue
            
    while True:
        if ser and ser.in_waiting > 0:
            try:
                line = ser.readline().decode('utf-8').strip()
                # Expected format: "ph:7.2,turb:1500,tds:200"
                if "ph:" in line:
                    parts = line.split(',')
                    for part in parts:
                        if "ph" in part: system_data["ph"] = float(part.split(':')[1])
                        if "turb" in part: system_data["turbidity"] = float(part.split(':')[1])
                        if "tds" in part: system_data["tds"] = float(part.split(':')[1])
                    
                    # Logic Check
                    if system_data["valve_state"] == "OPEN":
                        if system_data["turbidity"] > TURBIDITY_LIMIT: 
                            trigger_danger(f"High Turbidity: {system_data['turbidity']}")
                        elif system_data["ph"] < PH_LOW or system_data["ph"] > PH_HIGH: 
                            trigger_danger(f"Bad pH: {system_data['ph']}")
                    
                    # Send to Blynk
                    if BLYNK_AUTH and BLYNK_AUTH != "YOUR_BLYNK_AUTH_TOKEN":
                        blynk.virtual_write(5, system_data["ph"])
                        blynk.virtual_write(6, system_data["turbidity"])
                        blynk.virtual_write(7, system_data["tds"])
            except: pass
        time.sleep(0.5)

def load_ai():
    global interpreter, labels, input_details, output_details
    try:
        try: import tensorflow.lite.python.interpreter as tflite
        except: import tflite_runtime.interpreter as tflite
        with open(LABELS_PATH, 'r') as f: labels = [x.strip() for x in f.readlines()]
        interpreter = tflite.Interpreter(model_path=MODEL_PATH)
        interpreter.allocate_tensors()
        input_details = interpreter.get_input_details(); output_details = interpreter.get_output_details()
        return True
    except: return False

ai_enabled = load_ai()

def camera_loop():
    global output_frame, lock
    cap = cv2.VideoCapture(0)
    while True:
        ret, frame = cap.read()
        if not ret: time.sleep(0.1); continue
        
        # Display clone
        display = cv2.resize(frame, (640, 480))

        if ai_enabled and system_data["valve_state"] == "OPEN":
            try:
                img_in = cv2.resize(frame, (224, 224)); img_in = np.expand_dims(img_in, axis=0)
                if input_details[0]['dtype'] == np.float32: img_in = (np.float32(img_in)-127.5)/127.5
                interpreter.set_tensor(input_details[0]['index'], img_in); interpreter.invoke()
                out = np.squeeze(interpreter.get_tensor(output_details[0]['index']))
                idx = np.argmax(out); conf = out[idx]
                if output_details[0]['dtype'] == np.uint8: conf /= 255.0
                
                label = labels[idx] if labels else "Unknown"
                if conf > CONFIDENCE_THRESHOLD:
                    if "Danger" in label:
                        trigger_danger(f"Visual: {label}", frame)
                        cv2.putText(display, f"DANGER {int(conf*100)}%", (10,50), cv2.FONT_HERSHEY_SIMPLEX, 1, (0,0,255), 2)
                    else: 
                        cv2.putText(display, f"SAFE {int(conf*100)}%", (10,50), cv2.FONT_HERSHEY_SIMPLEX, 1, (0,255,0), 2)
            except: pass

        with lock: output_frame = display.copy()
    cap.release()

# ================= 4. FLASK WEB APP & CLOUDFLARE =================
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/status')
def get_status():
    return jsonify(system_data)

def generate_frames():
    global output_frame, lock
    while True:
        with lock:
            if output_frame is None: continue
            (flag, encoded) = cv2.imencode(".jpg", output_frame)
        yield(b'--frame\r\n' b'Content-Type: image/jpeg\r\n\r\n' + bytearray(encoded) + b'\r\n')

@app.route('/video_feed')
def video_feed(): return Response(generate_frames(), mimetype="multipart/x-mixed-replace; boundary=frame")

def start_cloudflare_tunnel():
    global public_url
    try:
        tunnel_cmd = ["cloudflared", "tunnel", "--url", "http://localhost:5000"]
        use_shell = True if os.name == 'nt' else False
        process = subprocess.Popen(tunnel_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, shell=use_shell)
        while True:
            line = process.stderr.readline()
            if not line: break
            match = re.search(r'https://[a-zA-Z0-9-]+\.trycloudflare\.com', line)
            if match:
                public_url = match.group(0)
                print(f"\n[WEB APP] 🌍 Dashboard URL: {public_url}\n")
                break
    except: pass

# ================= 5. MAIN STARTUP =================
if __name__ == "__main__":
    try: blynk = blynklib.Blynk(BLYNK_AUTH)
    except: pass
    
    threading.Thread(target=camera_loop, daemon=True).start()
    threading.Thread(target=serial_sensor_loop, daemon=True).start()
    threading.Thread(target=start_cloudflare_tunnel, daemon=True).start()
    
    def blynk_maintain():
        time.sleep(5)
        while True: 
            try: 
                blynk.run()
                if public_url: blynk.virtual_write(2, f"{public_url}") # Send Web Link to Blynk
            except: pass
            time.sleep(0.1)
    
    threading.Thread(target=blynk_maintain, daemon=True).start()
    
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)