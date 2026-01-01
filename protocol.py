"""
Protocol definitions for Jarvis Watch client-server communication.
Shared constants and message types used by both server and clients.
"""

import json
from enum import Enum
from dataclasses import dataclass, asdict
from typing import Optional, Any

# ============================================================================
# AUDIO FORMAT SPECIFICATIONS
# ============================================================================

# Input Audio (Client → Server)
INPUT_SAMPLE_RATE = 16000  # Hz
INPUT_BIT_DEPTH = 16  # bits
INPUT_CHANNELS = 1  # mono
INPUT_ENCODING = "pcm_s16le"  # little-endian
INPUT_CHUNK_SIZE = 1024  # bytes (512 samples @ 16-bit = ~32ms)

# Output Audio (Server → Client)
OUTPUT_SAMPLE_RATE = 24000  # Hz
OUTPUT_BIT_DEPTH = 16  # bits
OUTPUT_CHANNELS = 1  # mono
OUTPUT_ENCODING = "pcm_s16le"  # little-endian


# ============================================================================
# MESSAGE TYPES
# ============================================================================

class MessageType(str, Enum):
    """Message types for client-server communication."""
    
    # Client → Server
    SESSION_START = "session_start"
    AUDIO = "audio"
    END_OF_SPEECH = "end_of_speech"
    CANCEL = "cancel"
    PING = "ping"
    
    # Server → Client
    SESSION_STARTED = "session_started"
    SESSION_READY = "session_ready"
    TRANSCRIPT = "transcript"
    RESPONSE_TEXT = "response_text"
    AUDIO_RESPONSE = "audio_response"
    PROCESSING = "processing"
    DONE = "done"
    ERROR = "error"
    PONG = "pong"
    STATS = "stats"  # Detailed session statistics


# ============================================================================
# MESSAGE CLASSES
# ============================================================================

@dataclass
class BaseMessage:
    """Base class for all messages."""
    type: str
    
    def to_json(self) -> str:
        return json.dumps(asdict(self))
    
    @classmethod
    def from_json(cls, data: str | dict) -> "BaseMessage":
        if isinstance(data, str):
            data = json.loads(data)
        return cls(**data)


@dataclass
class SessionStartMessage(BaseMessage):
    """Client requests to start a voice session."""
    type: str = MessageType.SESSION_START.value
    
    # Optional client info
    client_id: Optional[str] = None
    client_type: Optional[str] = None  # "watch", "phone", "demo"


@dataclass
class AudioMessage(BaseMessage):
    """Audio data message (client → server or server → client)."""
    type: str = MessageType.AUDIO.value
    data: str = ""  # base64 encoded PCM audio
    sample_rate: int = INPUT_SAMPLE_RATE
    sequence: int = 0  # For ordering


@dataclass
class AudioResponseMessage(BaseMessage):
    """Audio response from server."""
    type: str = MessageType.AUDIO_RESPONSE.value
    data: str = ""  # base64 encoded PCM audio
    sample_rate: int = OUTPUT_SAMPLE_RATE
    is_final: bool = False


@dataclass
class TranscriptMessage(BaseMessage):
    """Real-time transcript update."""
    type: str = MessageType.TRANSCRIPT.value
    text: str = ""
    is_final: bool = False


@dataclass
class ResponseTextMessage(BaseMessage):
    """LLM response text chunk."""
    type: str = MessageType.RESPONSE_TEXT.value
    text: str = ""
    chunk_index: int = 0


@dataclass
class ProcessingMessage(BaseMessage):
    """Status update during processing."""
    type: str = MessageType.PROCESSING.value
    stage: str = ""  # "stt", "llm", "tts"
    message: str = ""


@dataclass
class DoneMessage(BaseMessage):
    """Session/response complete."""
    type: str = MessageType.DONE.value
    transcript: str = ""
    response: str = ""


@dataclass
class ErrorMessage(BaseMessage):
    """Error occurred."""
    type: str = MessageType.ERROR.value
    message: str = ""
    code: Optional[str] = None


@dataclass
class SessionStartedMessage(BaseMessage):
    """Server acknowledges session start."""
    type: str = MessageType.SESSION_STARTED.value
    session_id: str = ""


@dataclass
class SessionReadyMessage(BaseMessage):
    """Server is ready to receive audio."""
    type: str = MessageType.SESSION_READY.value
    message: str = "Ready for audio"


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def create_message(msg_type: MessageType, **kwargs) -> dict:
    """Create a message dictionary."""
    return {"type": msg_type.value, **kwargs}


def parse_message(data: str | bytes) -> dict:
    """Parse incoming message to dict."""
    if isinstance(data, bytes):
        data = data.decode('utf-8')
    return json.loads(data)


def is_binary_audio(data: bytes) -> bool:
    """Check if data is raw binary audio (not JSON)."""
    try:
        # If it parses as JSON, it's not raw binary
        json.loads(data)
        return False
    except (json.JSONDecodeError, UnicodeDecodeError):
        return True
