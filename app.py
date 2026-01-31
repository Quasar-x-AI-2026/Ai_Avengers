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
import re
from flask import Flask, Response, render_template_string, jsonify
from ultralytics import YOLO
from twilio.rest import Client
from math import hypot
from collections import Counter

app = Flask(__name__)

# --- CONFIGURATION ---
# UPDATE THIS PATH TO MATCH YOUR TESSERACT INSTALLATION
pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

TWILIO_ACCOUNT_SID = "AC69bd1a8455a8d8f020a42926781b1b17"
TWILIO_AUTH_TOKEN = "52c06151cc079571c4138284124f4686"
TWILIO_WHATSAPP_FROM = "whatsapp:+14155238886"
EMERGENCY_WHATSAPP_TO = "whatsapp:+919151757403"

CAMERA_SOURCE = 3
torch.set_num_threads(4)

# --- GLOBAL VARIABLES ---
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

# --- OCR THREADING VARIABLES ---
ocr_frame_buffer = None 
ocr_buffer_lock = threading.Lock()
latest_ocr_text = ""
ocr_stability_count = 0
OCR_STABILITY_THRESHOLD = 2 

print(">>> Loading Models... Please Wait.")

# --- MODEL LOADING ---
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

# --- FACE RECOGNITION SETUP ---
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
                    img = cv2.resize(img, (200, 200)) 
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

# --- AUDIO SETUP ---
try:
    speaker = win32com.client.Dispatch("SAPI.SpVoice")
    speaker.Rate = 1 
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

# --- HELPERS ---
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

# --- STRICT NOISE-FILTERING OCR ---
def run_fast_ocr(frame):
    try:
        # 1. Preprocessing
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (5, 5), 0)
        _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        
        # 2. Get Text
        raw_text = pytesseract.image_to_string(thresh, config="--psm 6")
        
        # 3. Strict Noise Filtering
        # Remove anything that isn't a letter or number
        clean_alphanum = re.sub(r'[^a-zA-Z0-9]', '', raw_text)
        
        # Rule A: Must be at least 4 characters long (prevents "I", "11", "a")
        if len(clean_alphanum) < 4: 
            return ""
            
        # Rule B: Must contain at least 3 valid LETTERS (prevents "88381" or "||||")
        letter_count = sum(c.isalpha() for c in clean_alphanum)
        if letter_count < 3:
            return ""

        # Rule C: If the string is just repeating characters (e.g. "SSSSS"), ignore it
        if len(set(clean_alphanum)) < 3:
            return ""

        # 4. Final Cleanup
        final_text = " ".join(re.sub(r'[^a-zA-Z0-9\s.,]', '', raw_text).split())
        return final_text.strip()
    except:
        return ""

# --- DEDICATED OCR THREAD ---
def ocr_background_worker():
    global latest_ocr_text, ocr_frame_buffer
    while True:
        if MODE == 4 and ocr_frame_buffer is not None:
            with ocr_buffer_lock:
                frame_to_process = ocr_frame_buffer.copy()
                ocr_frame_buffer = None 
            
            text = run_fast_ocr(frame_to_process)
            
            if text: latest_ocr_text = text
            else: latest_ocr_text = ""
        
        time.sleep(0.1) 

threading.Thread(target=ocr_background_worker, daemon=True).start()

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

# --- VOICE LISTENER (WITH SHUTDOWN FIX) ---
def background_voice_listener():
    r = sr.Recognizer()
    try:
        m = sr.Microphone()
    except OSError:
        print(">>> ERROR: No Microphone Found! Voice commands disabled.")
        return

    print(">>> Voice Listener Active")
    r.dynamic_energy_threshold = True
    r.energy_threshold = 300 
    r.pause_threshold = 0.8  

    with m as source:
        r.adjust_for_ambient_noise(source, duration=1.0)
        
        while True:
            try:
                audio = r.listen(source, phrase_time_limit=4.0)
                cmd = r.recognize_google(audio).lower()
                print(f">>> Voice Command: [{cmd}]") 

                if "obstacle" in cmd or "walk" in cmd: change_mode_logic(1)
                elif "currency" in cmd or "money" in cmd: change_mode_logic(2)
                elif "face" in cmd or "who" in cmd: change_mode_logic(3)
                elif "read" in cmd or "text" in cmd: change_mode_logic(4)
                elif "sos" in cmd or "help" in cmd: trigger_sos_logic()
                
                # --- UPDATED SHUTDOWN LOGIC ---
                elif "stop" in cmd:
                    print(">>> Stopping System...")
                    speak_async("System shutting down")
                    # Wait 3 seconds for the voice to finish speaking
                    time.sleep(3) 
                    # Kill the Python process completely
                    os._exit(0)

            except sr.WaitTimeoutError: pass 
            except sr.UnknownValueError: pass 
            except Exception as e: pass

def change_mode_logic(new_mode):
    global MODE, tracked_faces, name_history, next_id, stable_boxes, last_announced
    global latest_ocr_text, ocr_stability_count
    
    if MODE == new_mode: return 
    MODE = new_mode
    
    tracked_faces.clear(); name_history.clear(); next_id = 0
    stable_boxes.clear(); last_announced.clear()
    latest_ocr_text = ""; ocr_stability_count = 0
    
    modes = {1: "Obstacle", 2: "Currency", 3: "Face Recognition", 4: "Text Reading"}
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
    global latest_ocr_text, ocr_stability_count, ocr_frame_buffer

    previous_ocr_speak = ""

    while True:
        frame = cam.get_frame()
        if frame is None: 
            time.sleep(0.1)
            continue
        
        h, w = frame.shape[:2]
        now = time.time()
        current_detected = set()

        if MODE == 1: # OBSTACLE
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

        elif MODE == 2: # CURRENCY
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

        elif MODE == 3: # FACE
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

        elif MODE == 4: # READING (THREADED & FAST)
            bx1, by1, bx2, by2 = w//2-300, h//2-150, w//2+300, h//2+150
            cv2.rectangle(frame, (bx1, by1), (bx2, by2), (255,255,0), 2)
            
            with ocr_buffer_lock:
                ocr_frame_buffer = frame[by1:by2, bx1:bx2].copy()
            
            text = latest_ocr_text
            
            if text and text == previous_ocr_speak:
                ocr_stability_count += 1
            else:
                ocr_stability_count = 0
                previous_ocr_speak = text 
                
            if ocr_stability_count >= OCR_STABILITY_THRESHOLD:
                if len(text) > 4: 
                    current_detected.add(text)
                    if ocr_stability_count > 10: ocr_stability_count = 0

        if MODE != 3 and current_detected:
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
    <title>VISION VOICE | AI Assistant</title>
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet">
    <link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@500;700;900&family=Rajdhani:wght@300;500;700&display=swap" rel="stylesheet">
    <style>
        :root { 
            --bg-dark: #09090b;
            --panel-glass: rgba(20, 20, 25, 0.7);
            --primary: #00f3ff;
            --primary-glow: rgba(0, 243, 255, 0.4);
            --danger: #ff0f5b;
            --text-main: #ffffff;
            --text-muted: #8892b0;
            --sidebar-width: 340px;
            --border-radius: 12px;
        }

        body { 
            background: radial-gradient(circle at top right, #1a1a2e, var(--bg-dark));
            color: var(--text-main); 
            font-family: 'Rajdhani', sans-serif; 
            margin: 0; padding: 0; height: 100vh; 
            overflow: hidden; 
            display: flex; flex-direction: column; 
        }
        
        header { 
            height: 70px; 
            background: rgba(10, 10, 12, 0.8);
            backdrop-filter: blur(10px);
            border-bottom: 1px solid rgba(255,255,255,0.1); 
            display: flex; align-items: center; justify-content: space-between; 
            padding: 0 30px; 
            z-index: 20; flex-shrink: 0; 
            box-shadow: 0 4px 30px rgba(0,0,0,0.5);
        }

        .brand { display: flex; align-items: center; gap: 10px; }
        
        h1 { 
            font-family: 'Orbitron', sans-serif; 
            font-size: 1.8rem; margin: 0; 
            background: linear-gradient(90deg, #fff, var(--primary));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            font-weight: 800; letter-spacing: 2px;
        }

        .status-badge { 
            font-size: 0.85rem; 
            background: rgba(0, 243, 255, 0.1); 
            color: var(--primary); 
            padding: 6px 16px; 
            border-radius: 30px; 
            border: 1px solid var(--primary-glow);
            box-shadow: 0 0 10px var(--primary-glow);
            display: flex; align-items: center; gap: 8px;
            font-weight: 600; letter-spacing: 1px;
        }
        .status-dot { width: 8px; height: 8px; background: var(--primary); border-radius: 50%; animation: blink 2s infinite; }

        .dashboard { display: flex; flex: 1; width: 100%; height: calc(100vh - 70px); position: relative; }
        
        .video-container { 
            flex-grow: 1; background: #000; position: relative; 
            overflow: hidden; display: flex; align-items: center; justify-content: center;
        }

        .video-box { width: 100%; height: 100%; }
        
        img { width: 100%; height: 100%; object-fit: contain; display: block; }

        .overlay-info { 
            position: absolute; top: 20px; left: 20px; 
            background: rgba(0,0,0,0.6); backdrop-filter: blur(4px);
            padding: 15px; border-radius: 8px; 
            border-left: 4px solid var(--primary);
            color: rgba(255,255,255,0.9); 
            font-family: 'Orbitron', monospace; font-size: 0.9rem; 
            pointer-events: none; z-index: 5;
            box-shadow: 0 4px 15px rgba(0,0,0,0.5); line-height: 1.5;
        }
        .overlay-info span { color: var(--primary); font-weight: bold; }

        .expand-btn {
            position: absolute; top: 20px; right: 20px;
            background: rgba(255,255,255,0.1); 
            color: white; border: 1px solid rgba(255,255,255,0.2);
            width: 45px; height: 45px; border-radius: 50%; cursor: pointer;
            display: flex; align-items: center; justify-content: center; 
            transition: all 0.3s ease; z-index: 100; backdrop-filter: blur(4px);
        }
        .expand-btn:hover { background: var(--primary); color: #000; box-shadow: 0 0 15px var(--primary-glow); border-color: var(--primary); transform: scale(1.1); }

        .controls-panel { 
            width: var(--sidebar-width); 
            background: var(--panel-glass);
            backdrop-filter: blur(15px); -webkit-backdrop-filter: blur(15px);
            padding: 25px; display: flex; flex-direction: column; gap: 15px; 
            overflow-y: auto; 
            border-left: 1px solid rgba(255,255,255,0.08);
            transition: margin-right 0.4s cubic-bezier(0.175, 0.885, 0.32, 1.275);
            box-shadow: -5px 0 20px rgba(0,0,0,0.3); z-index: 10;
        }
        
        .dashboard.cinema-mode .controls-panel { margin-right: calc(var(--sidebar-width) * -1); }

        .panel-header { 
            font-family: 'Orbitron', sans-serif; color: var(--text-muted); 
            font-size: 0.8rem; margin-bottom: 5px; margin-top: 5px;
            text-transform: uppercase; letter-spacing: 2px; 
            border-bottom: 1px solid rgba(255,255,255,0.1); padding-bottom: 8px; 
        }

        .btn { 
            background: rgba(255,255,255,0.03); 
            border: 1px solid rgba(255,255,255,0.1); 
            color: var(--text-muted); 
            padding: 16px 20px; border-radius: var(--border-radius); 
            font-size: 1.1rem; font-family: 'Rajdhani', sans-serif; font-weight: 600;
            cursor: pointer; transition: all 0.3s ease; 
            display: flex; align-items: center; gap: 15px; 
            position: relative; overflow: hidden;
        }
        
        .btn i { font-size: 1.2rem; width: 25px; text-align: center; transition: 0.3s; }
        
        .btn:hover { 
            background: rgba(255,255,255,0.08); color: white; 
            transform: translateX(5px); border-color: rgba(255,255,255,0.3);
        }

        .btn.active { 
            background: rgba(0, 243, 255, 0.1); border: 1px solid var(--primary); 
            color: white; box-shadow: 0 0 20px rgba(0, 243, 255, 0.15); 
        }
        .btn.active i { color: var(--primary); text-shadow: 0 0 10px var(--primary); }

        .btn-sos { 
            margin-top: auto; 
            background: linear-gradient(135deg, #8b0000 0%, #d60045 100%); 
            border: none; color: white; 
            font-weight: 700; font-family: 'Orbitron', sans-serif; letter-spacing: 1px;
            justify-content: center; padding: 22px; 
            box-shadow: 0 4px 20px rgba(214, 0, 69, 0.3);
            animation: pulse-border 2s infinite;
        }
        .btn-sos:hover { transform: scale(1.02); box-shadow: 0 6px 25px rgba(214, 0, 69, 0.5); }

        .btn-quit { 
            background: transparent; border: 1px solid rgba(255,255,255,0.15); 
            color: #666; font-size: 0.9rem; padding: 12px; 
            justify-content: center; margin-bottom: 10px; 
        }
        .btn-quit:hover { border-color: #666; color: #fff; background: rgba(255,255,255,0.05); transform: none; }

        .log-box { 
            background: rgba(0,0,0,0.4); 
            border: 1px solid rgba(0, 243, 255, 0.2); padding: 15px; 
            font-family: 'Courier New', monospace; font-size: 0.85rem; 
            color: var(--primary); height: 90px; border-radius: var(--border-radius); 
            display: flex; flex-direction: column; justify-content: center;
            box-shadow: inset 0 0 20px rgba(0,0,0,0.5);
        }

        @keyframes blink { 0%, 100% { opacity: 1; box-shadow: 0 0 10px var(--primary); } 50% { opacity: 0.4; box-shadow: 0 0 0 transparent; } }
        @keyframes pulse-border { 0% { box-shadow: 0 0 0 0 rgba(214, 0, 69, 0.5); } 70% { box-shadow: 0 0 0 15px rgba(214, 0, 69, 0); } 100% { box-shadow: 0 0 0 0 rgba(214, 0, 69, 0); } }

        @media (max-width: 900px) { 
            .dashboard { flex-direction: column; overflow-y: auto; height: auto; } 
            body { overflow: auto; } 
            .video-container { height: 50vh; width: 100%; flex: none; } 
            .controls-panel { width: 100%; height: auto; border-left: none; border-top: 1px solid rgba(255,255,255,0.1); } 
            .expand-btn { display: none; }
        }
    </style>
</head>
<body>
    <header>
        <div class="brand">
            <i class="fas fa-eye" style="color:var(--primary); font-size: 1.5rem;"></i>
            <h1>VISION <span>ASSIST</span></h1>
        </div>
        <div class="status-badge"><div class="status-dot"></div> SYSTEM ONLINE</div>
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
            <div class="panel-header">MODE SELECTOR (KEYS 1-4)</div>
            
            <button class="btn active" id="btn-1" onclick="manualSetMode(1, 'Obstacle Detection', this)">
                <span style="font-family: Orbitron; color: var(--primary); font-weight: bold; width: 15px;">1</span> 
                <i class="fas fa-cube"></i> <div>Obstacle Mode</div>
            </button>
            
            <button class="btn" id="btn-2" onclick="manualSetMode(2, 'Currency Recognition', this)">
                <span style="font-family: Orbitron; color: var(--primary); font-weight: bold; width: 15px;">2</span>
                <i class="fas fa-rupee-sign"></i> <div>Currency Mode</div>
            </button>
            
            <button class="btn" id="btn-3" onclick="manualSetMode(3, 'Face Recognition', this)">
                <span style="font-family: Orbitron; color: var(--primary); font-weight: bold; width: 15px;">3</span>
                <i class="fas fa-user-shield"></i> <div>Face ID</div>
            </button>

            <button class="btn" id="btn-4" onclick="manualSetMode(4, 'Text Reader (OCR)', this)">
                <span style="font-family: Orbitron; color: var(--primary); font-weight: bold; width: 15px;">4</span>
                <i class="fas fa-book-reader"></i> <div>Text Reader</div>
            </button>

            <div class="panel-header" style="margin-top:10px;">ACTIONS (KEYS 5 & 0)</div>
            <div class="log-box" id="log-text">> System Initialized...</div>
            
            <button class="btn btn-sos" id="btn-5" onclick="triggerSOS()">
                <span style="position: absolute; left: 15px; font-family: Orbitron; opacity: 0.7;">5</span>
                <i class="fas fa-radiation"></i> SOS ALERT
            </button>
            
            <button class="btn btn-quit" id="btn-0" onclick="quitSystem()">
                <i class="fas fa-power-off"></i> POWER OFF (0)
            </button>
        </div>
    </div>

    <script>
        let currentMode = 1;
        const modeNames = {1: "Obstacle", 2: "Currency", 3: "Face", 4: "Text"};

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

        function manualSetMode(mode, name, element) {
            updateUIForMode(mode);
            fetch('/set_mode/' + mode).then(response => {
                updateLog(`Mode Set: ${modeNames[mode]}`);
            });
        }

        function updateUIForMode(mode) {
            if(currentMode === mode) return;
            currentMode = mode;
            
            document.getElementById('mode-display').innerText = modeNames[mode];
            document.querySelectorAll('.btn').forEach(b => b.classList.remove('active'));
            
            const btn = document.getElementById('btn-' + mode);
            if(btn) btn.classList.add('active');
        }

        function syncStatus() {
            fetch('/get_status')
                .then(r => r.json())
                .then(data => {
                    if (data.mode !== currentMode) {
                        updateUIForMode(data.mode);
                        updateLog(`Voice Sync: ${modeNames[data.mode]}`);
                    }
                })
                .catch(e => console.log("Sync error", e));
        }

        setInterval(syncStatus, 1000);

        function triggerSOS() { fetch('/sos').then(() => updateLog("⚠️ SOS SIGNAL SENT")); }
        
        function quitSystem() { 
            updateLog("SHUTTING DOWN..."); 
            fetch('/quit'); 
            setTimeout(() => { document.body.innerHTML = "<div style='color:white;text-align:center;margin-top:20%;font-family:Orbitron'><h1>SYSTEM OFFLINE</h1></div>"; }, 1000); 
        }

        document.addEventListener('keydown', function(event) {
            if(event.key === '1') document.getElementById('btn-1').click();
            if(event.key === '2') document.getElementById('btn-2').click();
            if(event.key === '3') document.getElementById('btn-3').click();
            if(event.key === '4') document.getElementById('btn-4').click();
            if(event.key === '5') document.getElementById('btn-5').click();
            if(event.key === '0') document.getElementById('btn-0').click();
            if(event.key === 'f' || event.key === 'F') toggleCinemaMode();
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

@app.route('/get_status')
def get_status():
    return jsonify(mode=MODE)

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
