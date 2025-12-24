# Jarvis Watch - Development Log

## Project Overview
Ultra low-latency voice assistant for smartwatch. Python simulator for testing before WatchOS deployment.

---

## 2024-12-23: Initial Setup

### Created Files
- `client.py` - Watch simulator (mic capture, VAD, WebSocket send, speaker playback)
- `server.py` - Demo echo server (receives audio, sends back)
- `requirements.txt` - Dependencies

### Architecture
```
Client (Watch Sim)              Server (Orchestrator)
┌─────────────────┐             ┌─────────────────┐
│ 🎤 Mic → VAD    │────────────►│ Receive audio   │
│                 │  WebSocket  │                 │
│ 🔊 Speaker      │◄────────────│ Send response   │
└─────────────────┘             └─────────────────┘
```

### Features
- **VAD**: Silero model with energy fallback
- **Toggle Activation**: Press 'A' to enable, auto-disable after speech
- **Full Duplex**: Record + playback simultaneously
- **Barge-in**: Clears playback when user speaks

---

## Audio Issue Fix

### Problem
- Distortion at chunk boundaries
- Speed variations during playback

### Root Cause
1. Variable chunk sizes from client
2. Async timing inconsistencies
3. Ring buffer causing gaps

### Solution
1. Server combines all audio before echo
2. Send in consistent chunk sizes
3. Proper timing based on sample rate: `chunk_bytes / (sample_rate * 2)`

---

## Usage

### Terminal 1 - Server
```bash
python server.py
```

### Terminal 2 - Client
```bash
python client.py
```

### Controls
| Key | Action |
|-----|--------|
| A | Toggle voice activation |
| Q | Quit |

---

## Config (client.py)
```python
SAMPLE_RATE = 16000    # Hz
CHANNELS = 1           # Mono
CHUNK_SIZE = 512       # frames
VAD_THRESHOLD = 0.5    # Silero confidence
MIN_SPEECH_MS = 250    # Minimum speech duration
MIN_SILENCE_MS = 500   # Silence to end speech
```

---

## WatchOS Mapping
| Simulator | WatchOS |
|-----------|---------|
| PyAudio mic | AVAudioEngine input |
| PyAudio speaker | AVAudioEngine output |
| Silero VAD | CoreML model |
| 'A' key | Digital Crown |
| WebSocket | URLSessionWebSocketTask |

---

## Dependencies
```
pyaudio
websockets
torch
numpy
pynput
```

Install: `pip install -r requirements.txt`

macOS requires: `brew install portaudio`
