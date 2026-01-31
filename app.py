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


pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

TWILIO_ACCOUNT_SID = "AC69bd1a8455a8d8f020a42926781b1b17"
TWILIO_AUTH_TOKEN = "2086af9ed51c2598963329e2e0732610"
TWILIO_WHATSAPP_FROM = "whatsapp:+14155238886"
EMERGENCY_WHATSAPP_TO = "whatsapp:+919151757403"

CAMERA_SOURCE = 1
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

ocr_frame_buffer = None 
ocr_buffer_lock = threading.Lock()
latest_ocr_text = ""
ocr_stability_count = 0
OCR_STABILITY_THRESHOLD = 2 

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

try:
    speaker = win32com.client.Dispatch("SAPI.SpVoice")
    speaker.Rate = 1 
    
    voices = speaker.GetVoices()
    for voice in voices:
        if "Zira" in voice.GetDescription():
            speaker.Voice = voice
            break
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

def run_fast_ocr(frame):
    try:
        
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (5, 5), 0)
        _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        
        
        raw_text = pytesseract.image_to_string(thresh, config="--psm 6")
        
        
        clean_alphanum = re.sub(r'[^a-zA-Z0-9]', '', raw_text)
        
       
        if len(clean_alphanum) < 4: 
            return ""
            
       
        letter_count = sum(c.isalpha() for c in clean_alphanum)
        if letter_count < 3:
            return ""

        if len(set(clean_alphanum)) < 3:
            return ""

        final_text = " ".join(re.sub(r'[^a-zA-Z0-9\s.,]', '', raw_text).split())
        return final_text.strip()
    except:
        return ""

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
                
                elif "stop" in cmd:
                    print(">>> Stopping System...")
                    speak_async("System shutting down")
                    time.sleep(3) 
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

        elif MODE == 3: 
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

elif MODE == 4: 
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








