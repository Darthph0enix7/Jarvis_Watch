# Project Jarvis - Voice Assistant Pipeline

A low-latency voice assistant with pluggable LLM providers.

## Architecture

```
┌─────────────┐    ┌─────────────┐    ┌─────────────┐
│   STT       │ →  │    LLM      │ →  │    TTS      │
│  (Cartesia) │    │ (Pluggable) │    │  (Cartesia) │
└─────────────┘    └─────────────┘    └─────────────┘
     ↑                                      ↓
  Microphone                            Speaker
```

## Project Structure

```
Jarvis_Watch/
├── main.py                 # Main pipeline orchestrator
├── mic_config.json         # Saved microphone selection
├── README.md               # This file
│
└── llm/                    # Pluggable LLM providers
    ├── __init__.py         # Module exports
    ├── base.py             # Abstract base class (BaseLLMClient)
    ├── gemini.py           # Google Gemini provider
    └── cerebras.py         # Cerebras provider (ultra-fast)
```

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

## Latency Breakdown

The "Silence → First Audio" latency consists of:

| Stage | Typical Time |
|-------|-------------|
| Silence wait (VAD) | ~1.0s (configurable) |
| STT finalization | ~0.2-0.3s |
| LLM time to first chunk | ~0.1-0.9s (varies by provider) |
| TTS time to first audio | ~0.3s |
| **Total** | **~1.6-2.5s** |

## Usage

```bash
python main.py
```

1. Select microphone (saved for future runs)
2. Wait for calibration
3. Speak your query
4. Wait for silence detection
5. Listen to response

## Dependencies

```
pyaudio
websockets
google-genai          # For Gemini
cerebras-cloud-sdk    # For Cerebras
```

## Statistics

After each session, detailed statistics are displayed:
- STT timing (speech duration, processing latency)
- LLM timing (time to first token, generation speed)
- TTS timing (time to first audio, total audio)
- End-to-end pipeline metrics
- Responsiveness rating
