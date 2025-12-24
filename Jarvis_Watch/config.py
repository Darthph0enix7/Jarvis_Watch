"""Central configuration for Jarvis Watch."""

import os

# API Keys
CARTESIA_API_KEY = os.environ.get("CARTESIA_API_KEY", "sk_car_eCzTSfNDxYxhgteweyBYr3")

# Voice
VOICE_ID = "ffe42012-140d-40ab-8cc3-d3f0e957dbc9"

# Audio
STT_SAMPLE_RATE = 16000
TTS_SAMPLE_RATE = 24000
CHUNK_SIZE = 512

# VAD
VAD_SILENCE_THRESHOLD = 0.01
VAD_SILENCE_DURATION = 1.5  # seconds

# Server
SERVER_HOST = "localhost"
SERVER_PORT = 8765

# Models
STT_MODEL = "ink-whisper"
TTS_MODEL = "sonic-3"
LLM_STREAM_DELAY = 0.05  # seconds per word (for mock)
