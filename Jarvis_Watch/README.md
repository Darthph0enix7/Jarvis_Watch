# Jarvis Watch

Voice assistant for smartwatch. Python simulator for testing.

## Architecture

```
Client (Watch)              Server (Orchestrator)
┌─────────────────┐         ┌─────────────────────────────────┐
│ 🎤 Mic → VAD    │────────►│ Audio → STT → LLM → TTS → Audio │
│                 │ WebSocket│                                 │
│ 🔊 Speaker      │◄────────│ Cartesia Ink → Mock → Sonic     │
└─────────────────┘         └─────────────────────────────────┘
```

## Files

| File | Purpose |
|------|---------|
| `config.py` | All parameters (models, VAD, server) |
| `stt.py` | Cartesia Ink speech-to-text |
| `tts.py` | Cartesia Sonic text-to-speech |
| `llm.py` | LLM placeholder (streaming mock) |
| `jarvis_server.py` | Server orchestrator |
| `jarvis_client.py` | Watch simulator |

## Setup

```bash
# Install dependencies
pip install -r requirements.txt

# macOS requires
brew install portaudio

# Set API key
export CARTESIA_API_KEY="your-key-here"
```

## Run

Terminal 1:
```bash
python jarvis_server.py
```

Terminal 2:
```bash
python jarvis_client.py
```

## Controls

| Key | Action |
|-----|--------|
| A | Start/stop recording |
| Q | Quit |

## Config

Edit `config.py` to change:

- **STT**: model, language, sample_rate, silence detection
- **TTS**: model, voice_id, sample_rate
- **LLM**: model, stream_delay, system_prompt
- **VAD**: threshold, min_speech_ms, min_silence_ms
- **Server**: host, port

Or override with environment variables and CLI args.
