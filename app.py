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


