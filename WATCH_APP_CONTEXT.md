# Project Jarvis - Watch App Context & Requirements

## Project Overview

**Project Jarvis** is a low-latency voice assistant with a client-server architecture:
- **Watch** = Input/Output device (microphone + speaker)
- **Server** = All processing (STT → LLM → TTS)

The goal is to create a voice assistant experience similar to talking to a real assistant, with minimal latency between speaking and hearing a response.

---

## Current Server Implementation (COMPLETED)

The server-side pipeline is fully implemented in Python and handles:

### 1. Speech-to-Text (STT)
- **Provider**: Cartesia Ink API (WebSocket streaming)
- **Model**: `ink-whisper`
- **Input**: Raw PCM audio (16kHz, 16-bit, mono)
- **Output**: Real-time transcript (partial + final)

### 2. LLM Processing
- **Architecture**: Pluggable provider system
- **Current Providers**: 
  - Google Gemini 2.5 Flash Lite
  - Cerebras (ultra-fast inference)
- **Features**: Streaming response with smart chunking for TTS

### 3. Text-to-Speech (TTS)
- **Provider**: Cartesia Sonic API (WebSocket streaming)
- **Model**: `sonic-3-2025-10-27`
- **Output**: Raw PCM audio (24kHz, 16-bit, mono)

### Current Latency Performance
| Metric | Time |
|--------|------|
| Silence detection | ~1.0s |
| STT finalization | ~0.2-0.3s |
| LLM first chunk | ~0.1-0.9s |
| TTS first audio | ~0.3s |
| **Total (silence → audio)** | **~1.6-2.5s** |

---

## Watch App Requirements

The watch acts as a **thin client** - it only handles audio I/O and streams data to/from the server.

### Watch Responsibilities

#### 1. Audio Input (Microphone)
- Capture audio from watch microphone
- Format: **16kHz sample rate, 16-bit PCM, mono**
- Stream audio chunks to server in real-time
- Chunk size: ~512 samples (~32ms per chunk) recommended

#### 2. Audio Output (Speaker)
- Receive PCM audio stream from server
- Format: **24kHz sample rate, 16-bit PCM, mono**
- Play audio in real-time as chunks arrive
- Handle buffering to prevent choppy playback

#### 3. Network Communication
- Maintain WebSocket connection to server
- Low-latency streaming in both directions
- Handle connection drops and reconnection

#### 4. UI/UX (Optional but recommended)
- Visual indicator when listening (recording)
- Visual indicator when processing
- Visual indicator when speaking (playing audio)
- Button to start/stop listening (or wake word detection)

---

## Communication Protocol (TO BE IMPLEMENTED)

The watch and server need to communicate over WebSocket. Here's the proposed protocol:

### Watch → Server Messages

```json
// Start a new voice session
{
  "type": "session_start"
}

// Stream audio chunk (binary or base64)
{
  "type": "audio",
  "data": "<base64_encoded_pcm_audio>"
}

// Or send raw binary audio frames directly

// End of speech (user stopped talking)
{
  "type": "end_of_speech"
}

// Cancel current session
{
  "type": "cancel"
}
```

### Server → Watch Messages

```json
// Session acknowledged
{
  "type": "session_started"
}

// Real-time transcript update (optional, for display)
{
  "type": "transcript",
  "text": "Hello how are you",
  "is_final": false
}

// LLM response text (optional, for display)
{
  "type": "response_text",
  "text": "I'm doing well, thanks for asking!"
}

// Audio chunk to play
{
  "type": "audio",
  "data": "<base64_encoded_pcm_audio>"
}

// Response complete
{
  "type": "done"
}

// Error occurred
{
  "type": "error",
  "message": "Error description"
}
```

---

## Audio Format Specifications

### Input Audio (Watch → Server)
| Property | Value |
|----------|-------|
| Sample Rate | 16,000 Hz |
| Bit Depth | 16-bit signed |
| Channels | Mono (1) |
| Encoding | PCM (raw bytes, little-endian) |
| Chunk Duration | ~32ms recommended |
| Chunk Size | 1024 bytes (512 samples) |

### Output Audio (Server → Watch)
| Property | Value |
|----------|-------|
| Sample Rate | 24,000 Hz |
| Bit Depth | 16-bit signed |
| Channels | Mono (1) |
| Encoding | PCM (raw bytes, little-endian) |

---

## Implementation Notes

### Voice Activity Detection (VAD)
Currently, the server handles VAD (detects when user stops speaking). Options:
1. **Server-side VAD** (current): Server detects 1s of silence and triggers processing
2. **Watch-side VAD**: Watch detects silence and sends `end_of_speech` message
3. **Hybrid**: Watch does rough VAD, server confirms

### Latency Optimization Tips
1. Start streaming audio immediately (don't wait for complete utterance)
2. Use binary WebSocket frames for audio (not base64 in JSON)
3. Keep WebSocket connection alive between sessions
4. Pre-buffer a small amount of audio on watch before playback starts

### Watch Platform Considerations
- **WearOS**: Use `AudioRecord` for input, `AudioTrack` for output
- **watchOS**: Use `AVAudioEngine` for both
- **Tizen**: Use `audio_in` and `audio_out` APIs

---

## Server Endpoint (TO BE IMPLEMENTED)

The server will expose a WebSocket endpoint:

```
ws://your-server:8080/voice
```

The current `main.py` needs to be wrapped with a WebSocket server (e.g., using `websockets` or `FastAPI`) to accept connections from the watch.

---

## Project Structure (Server Side)

```
Jarvis_Watch/
├── main.py                 # Current standalone pipeline
├── server.py               # [TODO] WebSocket server for watch
├── mic_config.json         # Local mic config (not needed for server mode)
├── README.md               # Project documentation
│
└── llm/                    # Pluggable LLM providers
    ├── __init__.py
    ├── base.py             # Abstract base class
    ├── gemini.py           # Google Gemini
    └── cerebras.py         # Cerebras (ultra-fast)
```

---

## What the Watch App Needs to Do (Summary)

1. **Connect** to server WebSocket
2. **Capture** audio from microphone (16kHz, 16-bit, mono)
3. **Stream** audio chunks to server in real-time
4. **Receive** audio chunks from server
5. **Play** audio chunks through speaker (24kHz, 16-bit, mono)
6. **Handle** UI states (listening, processing, speaking)
7. **Manage** connection lifecycle (reconnect on drop)

---

## Questions for Watch Implementation

1. What watch platform? (WearOS/watchOS/Tizen/other)
2. Wake word or button press to activate?
3. Should transcript/response text be displayed on watch?
4. Battery/performance constraints?
5. Offline fallback needed?

---

## Next Steps

1. Implement WebSocket server wrapper for current pipeline
2. Implement watch app audio capture and streaming
3. Implement watch app audio playback
4. Test end-to-end latency
5. Optimize for real-world conditions (network latency, etc.)
