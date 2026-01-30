# Blind Assistant System 👁️‍🗨️

An AI-powered assistive system for visually impaired users.  
It provides real-time obstacle detection, currency recognition,  
text reading (OCR), face recognition, and emergency SOS via WhatsApp  
using voice commands and a camera.

---

## Key Features

### 🧭 Obstacle Detection
- Uses YOLOv8 for real-time object detection
- Announces object name and position (left, center, right)
- Helps users navigate safely

### 💵 Currency Detection
- Identifies Indian currency denominations
- Uses a custom-trained YOLO model
- Prevents financial mistakes

### 📖 Text Reading (OCR)
- Captures text using camera
- Extracts text via Tesseract OCR
- Reads text aloud using text-to-speech

### 🙂 Face Recognition
- Recognizes known individuals using LBPH face recognition
- Announces names to the user
- Useful in social interactions

### 🚨 Emergency SOS
- Sends WhatsApp alert using Twilio API
- Includes live location and Google Maps link
- Voice-triggered for emergency situations

---

## Modes & Voice Commands

| Mode | Voice Command | Description |
|-----|--------------|-------------|
| Obstacle Detection | "Obstacle" | Detects nearby objects |
| Currency Detection | "Currency", "Money" | Identifies currency notes |
| Text Reading | "Read", "Text" | Reads printed text |
| Face Recognition | "Face", "Who is" | Recognizes known faces |
| Emergency SOS | "SOS", "Help" | Sends emergency alert |
| Exit | "Exit", "Quit" | Closes the application |


You can also switch modes using keyboard keys **(1–5)**.

---

## Technologies Used

- **Python** – Core programming language
- **OpenCV** – Image and video processing
- **YOLOv8 (Ultralytics)** – Real-time object detection
- **Tesseract OCR** – Text extraction from images
- **SpeechRecognition** – Voice command processing
- **Twilio WhatsApp API** – Emergency message delivery
- **PyTorch** – Deep learning framework
- **Windows SAPI** – Text-to-speech engine


---

## Requirements
- Windows OS
- Python 3.10 or 3.11
- Webcam and microphone
- Internet connection (for SOS)

---

## ⚙️ Installation


### 1️⃣ Clone the repository
git clone https://github.com/Quasar-x-AI-2026/Ai_Avengers

### 2️⃣ Install dependencies
pip install -r requirements.txt

### 3️⃣ Install Tesseract OCR

Download from:
https://github.com/tesseract-ocr/tesseract

Update the path in code:

pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

### 4️⃣ Run the project
python app.py

---

## Configuration

- Update Twilio credentials before use
- Change camera index source (0,1,2) if required 
- Ensure model files are in correct paths

---

### 🚨 SOS Configuration

This project uses Twilio WhatsApp API for emergency alerts.

Update the following variables in the code:

TWILIO_ACCOUNT_SID = "your_sid"

TWILIO_AUTH_TOKEN = "your_token"

TWILIO_WHATSAPP_FROM = "whatsapp:+14155238886"

EMERGENCY_WHATSAPP_TO = "whatsapp:+91XXXXXXXXXX"

---

## Future Improvements

- Mobile and wearable integration
- Offline speech recognition
- GPS-based navigation assistance
- Multilingual voice support

---


