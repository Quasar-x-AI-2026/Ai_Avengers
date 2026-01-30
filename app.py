import os
import cv2
import time
import threading
import numpy as np
import pytesseract
import speech_recognition as sr
import win32com.client
import torch
import requests
from flask import Flask, Response, render_template_string, jsonify
from ultralytics import YOLO
from twilio.rest import Client
from math import hypot
from collections import Counter

app = Flask(__name__)

pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

TWILIO_ACCOUNT_SID = "AC69bd1a8455a8d8f020a42926781b1b17"
TWILIO_AUTH_TOKEN = "52c06151cc079571c4138284124f4686"
TWILIO_WHATSAPP_FROM = "whatsapp:+14155238886"
EMERGENCY_WHATSAPP_TO = "whatsapp:+919151757403"

CAMERA_SOURCE = 2
torch.set_num_threads(4)

MODE = 1  
last_sos_time = 0
SOS_COOLDOWN = 30
lock = threading.Lock()

tracked_faces = {}
name_history = {}
next_id = 0
HISTORY_SIZE = 10
CONF_THRESHOLD = 85 

last_speak_time = 0
SPEAK_DELAY = 3
last_face_speak = 0
FACE_SPEAK_DELAY = 5
last_announced = set()

stable_boxes = {}
BOX_HOLD_TIME = 0.6
last_yolo_time = 0
YOLO_INTERVAL = 0.25

print(">>> Loading Models... Please Wait.")

try:
    object_model = YOLO("yolov8n.pt")
except:
    print("Downloading yolov8n.pt...")
    object_model = YOLO("yolov8n.pt") 

try:
    currency_model = YOLO("indian_currency.pt")
except:
    print(">>> Warning: 'indian_currency.pt' not found. Currency mode will default to object detection.")
    currency_model = None

face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')

DATASET_PATH = "dataset"
recognizer = cv2.face.LBPHFaceRecognizer_create()
label_map = {}
model_trained = False

def train_face_model():
    global label_map, model_trained, recognizer
    print(">>> Checking Dataset for Face Training...")
    
    if not os.path.exists(DATASET_PATH):
        os.makedirs(DATASET_PATH)
        print(f">>> Created '{DATASET_PATH}' folder. Add subfolders with photos (e.g. dataset/Rahul) to enable recognition.")
        return

    faces_list = []
    labels_list = []
    curr_label = 0
    temp_label_map = {}

    people = [p for p in os.listdir(DATASET_PATH) if os.path.isdir(os.path.join(DATASET_PATH, p))]
    
    if not people:
        print(">>> No faces found in 'dataset'. Face Recognition will treat everyone as Unknown.")
        return

    for person in people:
        path = os.path.join(DATASET_PATH, person)
        temp_label_map[curr_label] = person
        print(f"   - Training: {person}")
        
        for img_f in os.listdir(path):
            try:
                img_path = os.path.join(path, img_f)
                img = cv2.imread(img_path, cv2.IMREAD_GRAYSCALE)
                if img is not None:
                    img = cv2.resize(img, (200, 200)) # LBPH needs fixed size
                    faces_list.append(img)
                    labels_list.append(curr_label)
            except:
                continue
        curr_label += 1

    if faces_list:
        recognizer.train(faces_list, np.array(labels_list))
        label_map = temp_label_map
        model_trained = True
        print(f">>> Face Model Trained Successfully: {list(label_map.values())}")
    else:
        print(">>> No valid images found in dataset.")

train_face_model()

try:
    speaker = win32com.client.Dispatch("SAPI.SpVoice")
except:
    print("Warning: SAPI.SpVoice failed. Audio feedback disabled.")
    speaker = None

voice_lock = threading.Lock()

def speak_async(text):
    if speaker is None: return
    if voice_lock.locked(): return
    def run():
        with voice_lock: 
            try: speaker.Speak(text)
            except: pass
    threading.Thread(target=run, daemon=True).start()

def get_location():
    try:
        r = requests.get("https://ipinfo.io/json", timeout=3).json()
        loc = r.get("loc", "").split(",")
        if len(loc) == 2:
            return r.get("city", "Unknown"), f"http://maps.google.com/?q={loc[0]},{loc[1]}"
    except: pass
    return "Unknown", ""

def send_whatsapp_sos():
    def run():
        try:
            city, map_link = get_location()
            Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN).messages.create(
                body=f"🚨 *SOS ALERT*\nBlind Assistant User needs help!\n📍 {city}\n{map_link}",
                from_=TWILIO_WHATSAPP_FROM, to = EMERGENCY_WHATSAPP_TO
            )
            speak_async("Emergency Alert Sent")
        except Exception as e:
            print(f"SOS Failed: {e}")
    threading.Thread(target=run, daemon=True).start()

def run_ocr(frame):
    try:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        return pytesseract.image_to_string(thresh, config="--psm 6").strip()
    except Exception as e:
        return ""

class Camera:
    def __init__(self):
        print(f">>> Initializing Camera Source: {CAMERA_SOURCE}")
        self.cap = None
        self.connect(CAMERA_SOURCE)
        
        if not self.cap or not self.cap.isOpened():
            print(f">>> Source {CAMERA_SOURCE} failed. Falling back to Webcam 0...")
            self.connect(0)

    def connect(self, source):
        try:
            if isinstance(source, int) and os.name == 'nt':
                self.cap = cv2.VideoCapture(source, cv2.CAP_DSHOW)
            else:
                self.cap = cv2.VideoCapture(source)
            
            if self.cap.isOpened():
                self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
                self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        except Exception as e:
            print(f"Camera Error: {e}")

    def get_frame(self):
        if self.cap and self.cap.isOpened():
            ret, frame = self.cap.read()
            if ret: return frame
        return None

cam = Camera()

def background_voice_listener():
    r = sr.Recognizer()
    try:
        m = sr.Microphone()
    except OSError:
        print(">>> ERROR: No Microphone Found! Voice commands disabled.")
        return

    print(">>> Voice Listener Active (Say 'Help', 'Face', 'Obstacle')")

    with m as source:
        r.adjust_for_ambient_noise(source, duration=1)

    while True:
        try:
            with m as source:
                audio = r.listen(source, timeout=1, phrase_time_limit=4)
            
            cmd = r.recognize_google(audio).lower()
            print(f">>> Voice Command: [{cmd}]") 

            if "obstacle" in cmd or "walk" in cmd: change_mode_logic(1)
            elif "currency" in cmd or "money" in cmd: change_mode_logic(2)
            elif "read" in cmd or "text" in cmd: change_mode_logic(4)
            elif "face" in cmd or "who" in cmd: change_mode_logic(5)
            elif "sos" in cmd or "help" in cmd: trigger_sos_logic()
            elif "stop" in cmd: os._exit(0)

        except: pass 

def change_mode_logic(new_mode):
    global MODE, tracked_faces, name_history, next_id, stable_boxes, last_announced
    MODE = new_mode
    tracked_faces.clear(); name_history.clear(); next_id = 0
    stable_boxes.clear(); last_announced.clear()
    
    modes = {1: "Obstacle", 2: "Currency", 4: "Text Reading", 5: "Face Recognition"}
    print(f">>> Mode: {modes.get(new_mode)}")
    speak_async(f"{modes.get(new_mode)} mode")

def trigger_sos_logic():
    global last_sos_time
    if time.time() - last_sos_time > SOS_COOLDOWN:
        last_sos_time = time.time()
        speak_async("S O S Activated")
        send_whatsapp_sos()

threading.Thread(target=background_voice_listener, daemon=True).start()

def generate_frames():
    global last_yolo_time, tracked_faces, next_id, last_announced, last_speak_time, last_face_speak, stable_boxes

    while True:
        frame = cam.get_frame()
        if frame is None: 
            time.sleep(0.1)
            continue
        
        h, w = frame.shape[:2]
        now = time.time()
        current_detected = set()

        if MODE == 1:
            if now - last_yolo_time >= YOLO_INTERVAL:
                last_yolo_time = now
                res = object_model(frame, imgsz=640, conf=0.5, verbose=False)[0]
                for box in res.boxes:
                    x1, y1, x2, y2 = map(int, box.xyxy[0])
                    label = object_model.names[int(box.cls[0])]
                    cx = (x1+x2)//2
                    direction = "Left" if cx < w//3 else "Right" if cx > 2*w//3 else "Front"
                    
                    lbl = f"{label} {direction}"
                    current_detected.add(lbl)
                    stable_boxes[lbl] = (x1, y1, x2, y2, now)

        
            for lbl, (x1,y1,x2,y2,ts) in list(stable_boxes.items()):
                if now - ts > BOX_HOLD_TIME: del stable_boxes[lbl]; continue
                cv2.rectangle(frame, (x1,y1), (x2,y2), (0,255,0), 2)
                cv2.putText(frame, lbl, (x1, y1-10), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0,255,0), 2)

        elif MODE == 2:
            bx1, by1, bx2, by2 = w//2-200, h//2-150, w//2+200, h//2+150
            cv2.rectangle(frame, (bx1, by1), (bx2, by2), (0,255,255), 2)
            
            if now - last_yolo_time >= YOLO_INTERVAL:
                last_yolo_time = now
                roi = frame[by1:by2, bx1:bx2]
                
                model_to_use = currency_model if currency_model else object_model
                
                res = model_to_use(roi, imgsz=640, conf=0.6, verbose=False)[0]
                for box in res.boxes:
                    label = model_to_use.names[int(box.cls[0])]
                    current_detected.add(label)
                    cv2.putText(frame, label, (bx1, by1-10), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255,0,0), 2)

        elif MODE == 4:
            bx1, by1, bx2, by2 = w//2-300, h//2-150, w//2+300, h//2+150
            cv2.rectangle(frame, (bx1, by1), (bx2, by2), (255,255,0), 2)
            roi = frame[by1:by2, bx1:bx2] 
            text = run_ocr(roi)
            if len(text) > 3: current_detected.add(text)

        elif MODE == 5:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces_rects = face_cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(60, 60))
            
            new_tracked, names_frame = {}, set()

            for (x, y, ww, hh) in faces_rects:
                cx, cy = x + ww//2, y + hh//2
                
                mid = None
                for fid, (tx, ty) in tracked_faces.items():
                    if hypot(cx - tx, cy - ty) < 80:
                        mid = fid; break
                
                if mid is None:
                    mid = next_id
                    next_id += 1
                    name_history[mid] = []

                new_tracked[mid] = (cx, cy)
                
                pname = "Unknown"
                if model_trained:
                    roi = cv2.resize(gray[y:y+hh, x:x+ww], (200,200))
                    label, dist = recognizer.predict(roi)
                    if dist < CONF_THRESHOLD: 
                        pname = label_map.get(label, "Unknown")
                    else:
                        pname = "Unknown"
                
                name_history[mid].append(pname)
                if len(name_history[mid]) > HISTORY_SIZE: name_history[mid].pop(0)
                final_name = Counter(name_history[mid]).most_common(1)[0][0]
                
                names_frame.add(final_name)
                
                color = (0, 255, 0) if final_name != "Unknown" else (0, 0, 255)
                cv2.rectangle(frame, (x, y), (x+ww, y+hh), color, 2)
                cv2.putText(frame, final_name, (x, y-10), cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)

            tracked_faces = new_tracked
            
            if names_frame and now - last_face_speak > FACE_SPEAK_DELAY:
                valid_names = [n for n in names_frame if n != "Unknown"]
                if valid_names:
                    txt = f"{' and '.join(valid_names)} is here"
                    speak_async(txt)
                elif "Unknown" in names_frame:
                    speak_async("Unknown person detected")
                last_face_speak = now

        if MODE != 5 and current_detected:
            txt = " ".join(sorted(current_detected))
            if txt and (current_detected != last_announced or now - last_speak_time > SPEAK_DELAY):
                speak_async(txt)
                last_announced = current_detected.copy()
                last_speak_time = now

      
        try:
            ret, buffer = cv2.imencode('.jpg', frame)
            frame_bytes = buffer.tobytes()
            yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
        except: pass

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>VISION VOICE | Dashboard</title>
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
    <link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;700&family=Roboto:wght@300;400;700&display=swap" rel="stylesheet">
    <style>
        :root { 
            --bg-color: #0a0a0f; 
            --panel-bg: #111116; 
            --primary: #00f2ff; 
            --success: #00ff88; 
            --danger: #ff004c; 
            --text-main: #ffffff; 
            --text-dim: #8888aa; 
            --sidebar-width: 320px;
        }
        body { background-color: var(--bg-color); color: var(--text-main); font-family: 'Roboto', sans-serif; margin: 0; padding: 0; height: 100vh; overflow: hidden; display: flex; flex-direction: column; }
        
        /* HEADER */
        header { height: 60px; background: rgba(10, 10, 15, 0.95); border-bottom: 1px solid #333; display: flex; align-items: center; justify-content: space-between; padding: 0 20px; z-index: 10; flex-shrink: 0; }
        h1 { font-family: 'Orbitron', sans-serif; font-size: 1.5rem; color: var(--primary); margin: 0; letter-spacing: 2px; }
        h1 span { color: white; }
        .status-badge { font-size: 0.9rem; background: rgba(0, 242, 255, 0.1); color: var(--primary); padding: 5px 15px; border-radius: 20px; border: 1px solid var(--primary); }

        /* MAIN LAYOUT */
        .dashboard { display: flex; flex: 1; width: 100%; transition: all 0.3s ease; overflow: hidden; }
        
        .video-container { 
            flex-grow: 1; 
            background: #000; 
            position: relative; 
            overflow: hidden;
            display: flex;
            align-items: center;
            justify-content: center;
        }

        .video-box { 
            position: absolute; top: 0; left: 0; width: 100%; height: 100%; 
            background: #000;
        }
        
        img { 
            width: 100%; height: 100%; object-fit: contain; 
            display: block;
        }

        /* OVERLAYS */
        .overlay-info { 
            position: absolute; top: 15px; left: 15px; 
            background: rgba(0,0,0,0.5); padding: 10px; border-radius: 4px; border-left: 3px solid var(--primary);
            color: rgba(255,255,255,0.9); font-family: 'Courier New', monospace; font-size: 0.85rem; pointer-events: none;
            z-index: 5;
        }
        .expand-btn {
            position: absolute; top: 15px; right: 15px;
            background: rgba(0,0,0,0.6); color: white; border: 1px solid #444;
            width: 40px; height: 40px; border-radius: 50%; cursor: pointer;
            display: flex; align-items: center; justify-content: center; transition: 0.2s;
            z-index: 100;
        }
        .expand-btn:hover { background: var(--primary); color: black; border-color: var(--primary); }

        /* SIDEBAR */
        .controls-panel { 
            width: var(--sidebar-width); 
            flex-shrink: 0;
            background: var(--panel-bg); 
            padding: 20px; 
            display: flex; flex-direction: column; gap: 12px; 
            overflow-y: auto; 
            border-left: 1px solid #333;
            transition: margin-right 0.3s ease;
            height: 100%;
            box-sizing: border-box;
            z-index: 5;
        }
        
        .dashboard.cinema-mode .controls-panel { margin-right: calc(var(--sidebar-width) * -1); }

        /* CONTROLS */
        .panel-header { font-family: 'Orbitron', sans-serif; color: var(--text-dim); font-size: 0.75rem; margin-bottom: 5px; text-transform: uppercase; letter-spacing: 1px; border-bottom: 1px solid #333; padding-bottom: 5px; }
        .btn { background: #1a1a22; border: 1px solid #333; color: var(--text-dim); padding: 18px 15px; border-radius: 6px; font-size: 0.95rem; font-weight: 500; cursor: pointer; transition: all 0.2s ease; display: flex; align-items: center; gap: 12px; text-align: left; }
        .btn i { font-size: 1.2rem; width: 25px; text-align: center; }
        .btn:hover { background: #252530; color: white; transform: translateX(3px); border-left: 3px solid var(--primary); }
        .btn.active { background: rgba(0, 242, 255, 0.08); border: 1px solid var(--primary); color: white; box-shadow: 0 0 10px rgba(0, 242, 255, 0.1); }
        
        .btn-sos { margin-top: auto; background: linear-gradient(135deg, #8b0000, #ff004c); border: none; color: white; font-weight: bold; justify-content: center; animation: pulse 2s infinite; padding: 20px; }
        .btn-quit { background: #222; border: 1px solid #444; color: #888; font-size: 0.85rem; padding: 12px; justify-content: center; margin-bottom: 20px; }
        
        .log-box { background: #000; border: 1px solid #333; padding: 10px; font-family: 'Courier New', monospace; font-size: 0.75rem; color: var(--success); height: 80px; border-radius: 6px; overflow-y: hidden; display: flex; align-items: center; flex-shrink: 0; }
        
        @keyframes pulse { 0% { box-shadow: 0 0 0 0 rgba(255, 0, 76, 0.4); } 70% { box-shadow: 0 0 0 10px rgba(255, 0, 76, 0); } 100% { box-shadow: 0 0 0 0 rgba(255, 0, 76, 0); } }

        /* RESPONSIVE */
        @media (max-width: 900px) { 
            .dashboard { flex-direction: column; overflow-y: auto; } 
            body { overflow: auto; } 
            .video-container { height: 50vh; width: 100%; flex: none; } 
            .controls-panel { width: 100%; height: auto; border-left: none; border-top: 1px solid #333; overflow-y: visible; } 
            .expand-btn { display: none; }
        }
    </style>
</head>
<body>
    <header>
        <h1>VISION <span>ASSIST</span></h1>
        <div class="status-badge"><i class="fas fa-satellite-dish"></i> ONLINE</div>
    </header>
    
    <div class="dashboard" id="dashboard">
        <div class="video-container">
            <div class="overlay-info">
                CAM_01: ACTIVE<br>
                RES: 1280x720<br>
                MODE: <span id="mode-display">Obstacle</span>
            </div>
            <button class="expand-btn" onclick="toggleCinemaMode()" title="Toggle Fullscreen">
                <i class="fas fa-expand"></i>
            </button>
            <div class="video-box">
                <img src="{{ url_for('video_feed') }}" alt="Live Feed">
            </div>
        </div>

        <div class="controls-panel">
            <div class="panel-header">MODE SELECTOR</div>
            <button class="btn active" id="btn-1" onclick="setMode(1, 'Obstacle Detection', this)">
                <i class="fas fa-walking"></i> <div>Obstacle Mode</div>
            </button>
            <button class="btn" id="btn-2" onclick="setMode(2, 'Currency Recognition', this)">
                <i class="fas fa-coins"></i> <div>Currency Mode</div>
            </button>
            <button class="btn" id="btn-4" onclick="setMode(4, 'Text Reader (OCR)', this)">
                <i class="fas fa-book-open"></i> <div>Text Reader</div>
            </button>
            <button class="btn" id="btn-5" onclick="setMode(5, 'Face Recognition', this)">
                <i class="fas fa-user-check"></i> <div>Face ID</div>
            </button>

            <div class="panel-header" style="margin-top:10px;">SYSTEM LOG</div>
            <div class="log-box" id="log-text">> System Initialized...</div>
            
            <button class="btn btn-sos" onclick="triggerSOS()"><i class="fas fa-exclamation-triangle"></i> SOS ALERT</button>
            <button class="btn btn-quit" onclick="quitSystem()"><i class="fas fa-power-off"></i> SHUTDOWN</button>
        </div>
    </div>

    <script>
        function toggleCinemaMode() {
            const dash = document.getElementById('dashboard');
            dash.classList.toggle('cinema-mode');
            const icon = document.querySelector('.expand-btn i');
            if(dash.classList.contains('cinema-mode')) {
                icon.classList.remove('fa-expand'); icon.classList.add('fa-compress');
            } else {
                icon.classList.remove('fa-compress'); icon.classList.add('fa-expand');
            }
        }

        function updateLog(message) {
            const log = document.getElementById('log-text');
            const now = new Date().toLocaleTimeString();
            log.innerHTML = `> [${now}]<br>> ${message}`;
        }

        function setMode(mode, name, element) {
            document.getElementById('mode-display').innerText = name.split(' ')[0];
            fetch('/set_mode/' + mode).then(response => {
                updateLog(`Mode: ${name}`);
                document.querySelectorAll('.btn').forEach(b => b.classList.remove('active'));
                if(element) element.classList.add('active');
            });
        }

        function triggerSOS() { fetch('/sos').then(() => updateLog("⚠️ SOS SENT")); }
        
        function quitSystem() { 
            if(confirm("Shutdown System?")) { 
                updateLog("SHUTTING DOWN..."); 
                fetch('/quit'); 
                setTimeout(() => { document.body.innerHTML = "<div style='color:white;text-align:center;margin-top:20%'><h1>SYSTEM OFF</h1></div>"; }, 1000); 
            } 
        }

        document.addEventListener('keydown', function(event) {
            if(event.key === '1') document.getElementById('btn-1').click();
            if(event.key === '2') document.getElementById('btn-2').click();
            if(event.key === '4') document.getElementById('btn-4').click();
            if(event.key === '5') document.getElementById('btn-5').click();
            if(event.key === 'f' || event.key === 'F') toggleCinemaMode();
            if(event.key === ' ' || event.key === 'Enter') triggerSOS();
        });
    </script>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/video_feed')
def video_feed():
    return Response(generate_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/set_mode/<int:mode>')
def set_mode(mode):
    change_mode_logic(mode)
    return jsonify(success=True)

@app.route('/sos')
def sos():
    trigger_sos_logic()
    return jsonify(success=True)

@app.route('/quit')
def quit_app():
    speak_async("System shutting down")
    os._exit(0)

if __name__ == "__main__":
    print(">>> Starting Flask Server...")
    app.run(host='0.0.0.0', port=5000, debug=False)