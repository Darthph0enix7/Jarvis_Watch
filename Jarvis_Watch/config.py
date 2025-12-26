"""Central configuration for Jarvis Watch."""

import os

# API Keys
CARTESIA_API_KEY = os.environ.get("CARTESIA_API_KEY", "sk_car_eCzTSfNDxYxhgteweyBYr3")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "AIzaSyCg8amdduMspsehvE5UxLdB5lRlmC3Jm64")

# Voice
VOICE_ID = "ffe42012-140d-40ab-8cc3-d3f0e957dbc9" # jarvis 
#"228fca29-3a0a-435c-8728-5cb483251068"

# Audio
STT_SAMPLE_RATE = 16000
TTS_SAMPLE_RATE = 24000
CHUNK_SIZE = 512

# VAD - Optimized for low latency
VAD_SILENCE_THRESHOLD = 0.01
VAD_SILENCE_DURATION = 0.8  # Reduced from 1.5s - faster end-of-speech detection

# Server
SERVER_HOST = "localhost"
SERVER_PORT = 8765

# Models
STT_MODEL = "ink-whisper"
TTS_MODEL = "sonic-3"
GEMINI_MODEL = "gemini-2.0-flash"  # Faster model for lower latency
LLM_SYSTEM_PROMPT = "You are Jarvis, a helpful voice assistant. Keep responses concise (1-2 sentences) and natural for voice conversation."
