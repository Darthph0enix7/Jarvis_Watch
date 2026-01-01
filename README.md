# Project Jarvis - Voice Assistant Pipeline

A low-latency voice assistant with pluggable LLM providers and client-server architecture.

## Architecture

### Standalone Mode (main.py)
```
┌─────────────┐    ┌─────────────┐    ┌─────────────┐
│   STT       │ →  │    LLM      │ →  │    TTS      │
│  (Cartesia) │    │ (Pluggable) │    │  (Cartesia) │
└─────────────┘    └─────────────┘    └─────────────┘
     ↑                                      ↓
  Microphone                            Speaker
```

### Client-Server Mode (server.py + clients)
```
┌──────────────────┐         ┌──────────────────────────────────────┐
│      CLIENT      │  WS     │              SERVER                  │
│  (Watch/Phone)   │ ◄────►  │  STT → LLM → TTS                     │
│                  │         │                                      │
│  🎤 Mic Input    │ ────►   │  Process voice, generate response    │
│  🔊 Speaker Out  │ ◄────   │  Stream audio back                   │
└──────────────────┘         └──────────────────────────────────────┘
```

## Project Structure

```
Jarvis_Watch/
├── main.py                 # Standalone mode (local mic + speaker)
├── server.py               # WebSocket server for remote clients
├── demo_client.py          # Demo client for testing on same machine
├── protocol.py             # Shared protocol definitions
├── PROTOCOL_SPEC.md        # Full protocol documentation for client devs
├── mic_config.json         # Saved microphone selection
├── session_stats.jsonl     # Session statistics log (auto-generated)
├── README.md               # This file
├── WATCH_APP_CONTEXT.md    # Watch app requirements & protocol spec
│
└── llm/                    # Pluggable LLM providers
    ├── __init__.py         # Module exports
    ├── base.py             # Abstract base class (BaseLLMClient)
    ├── gemini.py           # Google Gemini provider
    └── cerebras.py         # Cerebras provider (ultra-fast)
```

## Quick Start

### Option 1: Standalone Mode (Local)
Run everything locally with your computer's mic and speaker:
```bash
python main.py
```

### Option 2: Client-Server Mode

**Terminal 1 - Start the server:**
```bash
python server.py --host 0.0.0.0 --port 8080 --llm cerebras --token your_secret_token
```

**Terminal 2 - Run the demo client:**
```bash
python demo_client.py --server ws://localhost:8080 --token your_secret_token
```

### Server Options
```bash
python server.py --help

Options:
  --host HOST           Host to bind to (default: 0.0.0.0)
  --port PORT           Port to bind to (default: 8080)
  --llm {gemini,cerebras}  LLM provider (default: cerebras)
  --token TOKEN         Authentication token (default: jarvis_secret_2024)
```

### Demo Client Options
```bash
python demo_client.py --help

Options:
  --server URL    Server WebSocket URL (default: ws://localhost:8080)
  --token TOKEN   Authentication token (default: jarvis_secret_2024)
  --device INDEX  Audio input device index (prompts if not specified)
```

### Demo Client Controls
- Press **ENTER** to start recording
- Press **ENTER** again to stop (or wait for auto-silence detection)
- Type **'q' + ENTER** to quit

## Authentication

The server uses token-based authentication for security:

```bash
# Set via command line
python server.py --token my_secure_password

# Or via environment variable
export JARVIS_AUTH_TOKEN="my_secure_password"
python server.py
```

Clients connect with token in URL:
```
ws://your-server:8080?token=my_secure_password
```

## Protocol Documentation

See **[PROTOCOL_SPEC.md](PROTOCOL_SPEC.md)** for complete documentation including:
- WebSocket connection and authentication
- Audio format specifications (input/output)
- All message types and schemas
- Session lifecycle and state machine
- Statistics and latency metrics
- Android/Kotlin implementation guide
- Code examples for multiple platforms

## Components

### 1. Speech-to-Text (STT)
- **Provider**: Cartesia Ink API
- **Model**: `ink-whisper`
- **Features**:
  - Real-time WebSocket streaming
  - Voice Activity Detection (VAD) with ambient noise calibration
  - Auto-stop on silence (configurable duration)
  - Partial + final transcript display

### 2. LLM (Language Model)
- **Architecture**: Pluggable provider system
- **Available Providers**:
  - `gemini` - Google Gemini 2.5 Flash Lite
  - `cerebras` - Cerebras (ultra-fast inference)
- **Features**:
  - Streaming response with smart chunking for TTS
  - Optimized chunk sizes (2 words first, 6 words subsequent)
  - Natural break point detection (sentences, commas)

### 3. Text-to-Speech (TTS)
- **Provider**: Cartesia Sonic API
- **Model**: `sonic-3-2025-10-27`
- **Features**:
  - Real-time WebSocket streaming
  - Parallel LLM→TTS pipeline (chunks sent as generated)
  - 24kHz PCM audio output

## Configuration

Edit `main.py` to configure:

```python
# LLM Provider Selection
LLM_PROVIDER = "cerebras"  # Options: "gemini", "cerebras"

# API Keys
CARTESIA_API_KEY = "your-key"
GEMINI_API_KEY = "your-key"
CEREBRAS_API_KEY = "your-key"

# VAD Settings
SILENCE_DURATION = 1.0  # Seconds of silence before auto-stop
```

## Adding New LLM Providers

1. Create `llm/your_provider.py`:

```python
from .base import BaseLLMClient
from typing import AsyncIterator

class YourLLMClient(BaseLLMClient):
    def __init__(self, api_key: str, model: str):
        super().__init__()
        # Initialize your client
    
    @property
    def provider_name(self) -> str:
        return "YourProvider"
    
    async def _stream_raw_tokens(self, prompt: str) -> AsyncIterator[str]:
        # Yield tokens as they arrive
        for token in your_api_stream():
            yield token
```

2. Add to `llm/__init__.py`:
```python
from .your_provider import YourLLMClient
```

3. Add to factory in `main.py`:
```python
elif provider == "your_provider":
    return YourLLMClient(api_key=YOUR_API_KEY, model=YOUR_MODEL)
```

## Statistics & Metrics

### Automatic Logging
All session statistics are automatically saved to `session_stats.jsonl` in JSON Lines format:

```bash
# View recent sessions
tail -5 session_stats.jsonl | python -m json.tool

# Analyze with Python
import json
with open('session_stats.jsonl') as f:
    sessions = [json.loads(line) for line in f]
    avg_latency = sum(s['latencies']['voice_response_latency_ms'] for s in sessions) / len(sessions)
    print(f"Average voice response latency: {avg_latency}ms")
```

### Key Metrics Tracked
| Metric | Description |
|--------|-------------|
| `voice_response_latency_ms` | ⭐ Silence → First audio (key UX metric) |
| `time_to_first_llm_token_ms` | LLM response start time |
| `tts_latency_ms` | TTS audio generation time |
| `stt_processing_ms` | Speech recognition time |
| `total_session_ms` | Full session duration |

### Latency Breakdown

The "Silence → First Audio" latency consists of:

| Stage | Typical Time |
|-------|-------------|
| Silence wait (VAD) | ~1.0s (configurable) |
| STT finalization | ~0.2-0.3s |
| LLM time to first chunk | ~0.1-0.9s (varies by provider) |
| TTS time to first audio | ~0.3s |
| **Total** | **~1.6-2.5s** |

### Responsiveness Rating
- ⭐⭐⭐ **Excellent**: < 1.5 seconds
- ⭐⭐ **Good**: < 2.5 seconds
- ⭐ **Needs improvement**: > 2.5 seconds

## Building Clients

### Android/WearOS
See [PROTOCOL_SPEC.md](PROTOCOL_SPEC.md) for detailed Kotlin implementation guide.

Quick summary:
1. Connect: `ws://server:8080?token=xxx`
2. Send: `{"type": "session_start", "client_type": "watch"}`
3. Wait for: `{"type": "session_ready"}`
4. Stream audio: Raw PCM bytes (16kHz, 16-bit, mono)
5. Signal done: `{"type": "end_of_speech"}`
6. Receive: Binary audio (24kHz) + JSON messages

### Audio Formats
| Direction | Sample Rate | Bit Depth | Channels | Encoding |
|-----------|-------------|-----------|----------|----------|
| Input (mic) | 16,000 Hz | 16-bit | Mono | PCM S16LE |
| Output (speaker) | 24,000 Hz | 16-bit | Mono | PCM S16LE |

## Dependencies

```
pyaudio
websockets
google-genai          # For Gemini
cerebras-cloud-sdk    # For Cerebras
```

## Troubleshooting

### Connection Failed (401)
- Check your authentication token matches server and client

### No Audio Received
- Verify microphone permissions
- Check audio format (must be 16kHz PCM)
- Ensure server has network access to Cartesia APIs

### High Latency
- Use Cerebras LLM provider (fastest)
- Reduce `SILENCE_DURATION` if acceptable
- Check network latency to server
