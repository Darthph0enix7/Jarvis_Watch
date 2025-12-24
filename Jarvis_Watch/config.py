"""Configuration - All parameters in one place."""

import os
from dataclasses import dataclass

@dataclass
class STTConfig:
    model: str = "ink-whisper"
    language: str = "en"
    encoding: str = "pcm_s16le"
    sample_rate: int = 16000
    min_volume: float = 0.1
    max_silence_duration: float = 0.7

@dataclass
class TTSConfig:
    model: str = "sonic-3"
    voice_id: str = "ffe42012-140d-40ab-8cc3-d3f0e957dbc9"
    encoding: str = "pcm_s16le"
    sample_rate: int = 24000
    chunk_max_chars: int = 80

@dataclass
class LLMConfig:
    model: str = "gpt-4"  # Placeholder
    stream_delay: float = 0.05  # Simulated token delay
    system_prompt: str = "You are Jarvis, a helpful voice assistant. Keep responses concise."

@dataclass
class VADConfig:
    threshold: float = 0.5
    min_speech_ms: int = 250
    min_silence_ms: int = 500

@dataclass
class ServerConfig:
    host: str = "localhost"
    port: int = 8765

@dataclass
class ClientConfig:
    chunk_size: int = 512
    reconnect_delay: float = 2.0
    max_reconnects: int = 10

@dataclass
class Config:
    stt: STTConfig
    tts: TTSConfig
    llm: LLMConfig
    vad: VADConfig
    server: ServerConfig
    client: ClientConfig
    cartesia_api_key: str
    
    @classmethod
    def load(cls) -> "Config":
        api_key = os.environ.get("CARTESIA_API_KEY", "")
        if not api_key:
            raise ValueError("CARTESIA_API_KEY environment variable required")
        
        return cls(
            stt=STTConfig(),
            tts=TTSConfig(),
            llm=LLMConfig(),
            vad=VADConfig(),
            server=ServerConfig(),
            client=ClientConfig(),
            cartesia_api_key=api_key
        )

# Global config instance
_config: Config = None

def get_config() -> Config:
    global _config
    if _config is None:
        _config = Config.load()
    return _config
