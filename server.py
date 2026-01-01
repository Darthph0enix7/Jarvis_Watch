"""
Jarvis Voice Assistant - WebSocket Server

This server handles the voice assistant pipeline:
- Receives audio streams from clients (watch, phone, demo)
- Processes through STT → LLM → TTS pipeline
- Streams audio responses back to clients

Usage:
    python server.py [--host HOST] [--port PORT] [--llm PROVIDER] [--token TOKEN]

Example:
    python server.py --host 0.0.0.0 --port 8000 --llm cerebras    #--token mysecret

Environment Variables:
    JARVIS_AUTH_TOKEN - Authentication token for clients
"""

import asyncio
import json
import sys
import os
import time
import struct
import base64
import argparse
import uuid
import hashlib
import secrets
from typing import Optional
from urllib.parse import urlencode, parse_qs, urlparse
from dataclasses import dataclass, field, asdict
from datetime import datetime

import websockets
from websockets.server import WebSocketServerProtocol
from websockets.http import Headers
from websockets.asyncio.server import Response

# Import LLM providers
from llm import GeminiLLMClient, CerebrasLLMClient, BaseLLMClient

# Import protocol definitions
from protocol import (
    MessageType, INPUT_SAMPLE_RATE, OUTPUT_SAMPLE_RATE,
    create_message, parse_message, is_binary_audio
)

# ============================================================================
# CONFIGURATION
# ============================================================================

# Authentication
AUTH_TOKEN = os.environ.get("JARVIS_AUTH_TOKEN", "Denemeler123.")

# API Keys (load from environment or use defaults for development)
CARTESIA_API_KEY = os.environ.get("CARTESIA_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
CEREBRAS_API_KEY = os.environ.get("CEREBRAS_API_KEY")

# Statistics storage
STATS_FILE = "session_stats.jsonl"  # JSON Lines format for easy appending

# Cartesia endpoints
CARTESIA_STT_URL = "wss://api.cartesia.ai/stt/websocket"
CARTESIA_TTS_URL = "wss://api.cartesia.ai/tts/websocket"

# TTS configuration
TTS_VOICE_ID = "ffe42012-140d-40ab-8cc3-d3f0e957dbc9"
TTS_MODEL = "sonic-3-2025-10-27"
TTS_SPEED = 1.2  # Range: 0.6 to 1.5 (multiplier on default speed)
TTS_VOLUME = 1.5  # Range: 0.5 to 2.0 (multiplier on default volume)
TTS_EMOTION = "neutral"  # Options: neutral, angry, excited, content, sad, scared, etc.

# LLM configuration
GEMINI_MODEL = "models/gemini-2.5-flash-lite"
CEREBRAS_MODEL = "gpt-oss-120b"

# VAD settings
SILENCE_DURATION = 1.0  # seconds
VAD_THRESHOLD = 300  # default RMS threshold

# Cartesia STT parameters
CARTESIA_PARAMS = {
    "api_key": CARTESIA_API_KEY,
    "cartesia_version": "2024-06-10",
    "model": "ink-whisper",
    "language": "en",
    "encoding": "pcm_s16le",
    "sample_rate": str(INPUT_SAMPLE_RATE),
    "min_volume": "0.1",
    "max_silence_duration_secs": "2.0",
}


# ============================================================================
# LLM FACTORY
# ============================================================================

def create_llm_client(provider: str = "cerebras") -> BaseLLMClient:
    """Factory function to create LLM client based on provider."""
    if provider == "gemini":
        return GeminiLLMClient(api_key=GEMINI_API_KEY, model=GEMINI_MODEL)
    elif provider == "cerebras":
        return CerebrasLLMClient(api_key=CEREBRAS_API_KEY, model=CEREBRAS_MODEL)
    else:
        raise ValueError(f"Unknown LLM provider: {provider}")


# ============================================================================
# SESSION STATISTICS
# ============================================================================

@dataclass
class SessionStatistics:
    """Comprehensive statistics tracking for a voice session."""
    
    session_id: str = ""
    client_type: str = ""
    llm_provider: str = ""
    llm_model: str = ""
    
    # Timestamps (all in Unix epoch seconds)
    timestamps: dict = field(default_factory=lambda: {
        'session_start': None,
        'stt_connected': None,
        'tts_connected': None,
        'first_audio_received': None,
        'speech_start': None,
        'first_transcript': None,
        'speech_end': None,
        'final_transcript': None,
        'llm_request_sent': None,
        'first_llm_token': None,
        'first_llm_chunk': None,
        'first_tts_chunk_sent': None,
        'first_audio_response': None,
        'llm_complete': None,
        'last_audio_response': None,
        'session_end': None,
    })
    
    # Metrics
    metrics: dict = field(default_factory=lambda: {
        'input_word_count': 0,
        'output_word_count': 0,
        'output_char_count': 0,
        'transcript_updates': 0,
        'final_segments': 0,
        'llm_chunks_generated': 0,
        'llm_tokens': [],  # Token timestamps for inter-token latency
        'llm_chunk_times': [],  # Chunk timestamps
        'audio_chunks_sent': 0,
        'audio_bytes_sent': 0,
        'audio_chunks_received': 0,
        'audio_bytes_received': 0,
    })
    
    # Content
    transcript: str = ""
    response: str = ""
    
    def compute_latencies(self) -> dict:
        """Compute all latency metrics in milliseconds."""
        ts = self.timestamps
        latencies = {}
        
        def ms_diff(end_key: str, start_key: str) -> Optional[int]:
            if ts.get(end_key) and ts.get(start_key):
                return int((ts[end_key] - ts[start_key]) * 1000)
            return None
        
        latencies['stt_connect_ms'] = ms_diff('stt_connected', 'session_start')
        latencies['tts_connect_ms'] = ms_diff('tts_connected', 'session_start')
        latencies['time_to_first_transcript_ms'] = ms_diff('first_transcript', 'speech_start')
        latencies['stt_processing_ms'] = ms_diff('final_transcript', 'speech_end')
        latencies['time_to_first_llm_token_ms'] = ms_diff('first_llm_token', 'llm_request_sent')
        latencies['time_to_first_llm_chunk_ms'] = ms_diff('first_llm_chunk', 'llm_request_sent')
        latencies['tts_latency_ms'] = ms_diff('first_audio_response', 'first_tts_chunk_sent')
        latencies['voice_response_latency_ms'] = ms_diff('first_audio_response', 'speech_end')
        latencies['total_response_latency_ms'] = ms_diff('last_audio_response', 'speech_end')
        latencies['total_session_ms'] = ms_diff('session_end', 'session_start')
        
        # Compute speech duration
        speech_duration_ms = ms_diff('speech_end', 'speech_start')
        if speech_duration_ms:
            latencies['speech_duration_ms'] = speech_duration_ms
        
        # Remove None values
        return {k: v for k, v in latencies.items() if v is not None}
    
    def compute_metrics(self) -> dict:
        """Compute all metrics for the session."""
        m = self.metrics.copy()
        
        # Word counts
        if self.transcript:
            m['input_word_count'] = len(self.transcript.split())
        if self.response:
            m['output_word_count'] = len(self.response.split())
            m['output_char_count'] = len(self.response)
        
        # Speaking rate
        speech_duration = None
        if self.timestamps.get('speech_end') and self.timestamps.get('speech_start'):
            speech_duration = self.timestamps['speech_end'] - self.timestamps['speech_start']
            if speech_duration > 0 and m['input_word_count'] > 0:
                m['speaking_rate_wpm'] = int(m['input_word_count'] / speech_duration * 60)
        
        # Inter-chunk latency
        chunk_times = m.get('llm_chunk_times', [])
        if len(chunk_times) > 1:
            intervals = [chunk_times[i] - chunk_times[i-1] for i in range(1, len(chunk_times))]
            m['avg_inter_chunk_ms'] = int(sum(intervals) / len(intervals) * 1000)
        
        # Clean up internal tracking fields
        m.pop('llm_tokens', None)
        m.pop('llm_chunk_times', None)
        
        # Add provider info
        m['llm_provider'] = self.llm_provider
        m['llm_model'] = self.llm_model
        
        return m
    
    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            'session_id': self.session_id,
            'client_type': self.client_type,
            'timestamp': datetime.now().isoformat(),
            'timestamps': {k: v for k, v in self.timestamps.items() if v is not None},
            'latencies': self.compute_latencies(),
            'metrics': self.compute_metrics(),
            'transcript': self.transcript,
            'response': self.response,
        }
    
    def to_message(self) -> dict:
        """Create a stats message for client."""
        return create_message(
            MessageType.STATS,
            session_id=self.session_id,
            timestamps=self.timestamps,
            latencies=self.compute_latencies(),
            metrics=self.compute_metrics()
        )
    
    def save_to_file(self, filepath: str = STATS_FILE) -> None:
        """Append stats to JSON Lines file."""
        with open(filepath, 'a') as f:
            f.write(json.dumps(self.to_dict()) + '\n')
    
    def display(self) -> None:
        """Display formatted statistics to console."""
        latencies = self.compute_latencies()
        metrics = self.compute_metrics()
        
        print("\n" + "=" * 60)
        print(f"📊 SESSION STATISTICS [{self.session_id}]")
        print("=" * 60)
        
        # STT Timing
        print("\n⏱️  STT TIMING:")
        if latencies.get('speech_duration_ms'):
            print(f"   Speech Duration:           {latencies['speech_duration_ms']/1000:.2f}s")
        if latencies.get('time_to_first_transcript_ms'):
            print(f"   Time to First Transcript:  {latencies['time_to_first_transcript_ms']}ms")
        if latencies.get('stt_processing_ms'):
            print(f"   STT Processing Latency:    {latencies['stt_processing_ms']}ms")
        if latencies.get('stt_connect_ms'):
            print(f"   WebSocket Connect Time:    {latencies['stt_connect_ms']}ms")
        
        # LLM Timing
        print("\n🤖 LLM TIMING:")
        if latencies.get('time_to_first_llm_token_ms'):
            print(f"   Time to First Token:       {latencies['time_to_first_llm_token_ms']}ms")
        if latencies.get('time_to_first_llm_chunk_ms'):
            print(f"   Time to First Chunk:       {latencies['time_to_first_llm_chunk_ms']}ms")
        if metrics.get('avg_inter_chunk_ms'):
            print(f"   Avg Inter-Chunk Delay:     {metrics['avg_inter_chunk_ms']}ms")
        
        # TTS Timing
        print("\n🔊 TTS TIMING:")
        if latencies.get('tts_latency_ms'):
            print(f"   Time to First Audio:       {latencies['tts_latency_ms']}ms")
        if latencies.get('tts_connect_ms'):
            print(f"   TTS Connect Time:          {latencies['tts_connect_ms']}ms")
        print(f"   Audio Chunks Received:     {metrics.get('audio_chunks_received', 0)}")
        audio_kb = metrics.get('audio_bytes_received', 0) / 1024
        print(f"   Total Audio Output:        {audio_kb:.1f} KB")
        
        # End-to-End
        print("\n🔄 END-TO-END PIPELINE:")
        if latencies.get('voice_response_latency_ms'):
            print(f"   Voice Response Latency:    {latencies['voice_response_latency_ms']}ms ⭐")
            # Rating
            vrl = latencies['voice_response_latency_ms']
            if vrl < 1500:
                print(f"   Rating:                    ⭐⭐⭐ Excellent (<1.5s)")
            elif vrl < 2500:
                print(f"   Rating:                    ⭐⭐ Good (<2.5s)")
            else:
                print(f"   Rating:                    ⭐ Needs improvement (>2.5s)")
        if latencies.get('total_session_ms'):
            print(f"   Total Session Duration:    {latencies['total_session_ms']/1000:.2f}s")
        
        # Transcript Stats
        print("\n📝 TRANSCRIPT:")
        print(f"   Input Words:               {metrics.get('input_word_count', 0)}")
        if metrics.get('speaking_rate_wpm'):
            print(f"   Speaking Rate:             {metrics['speaking_rate_wpm']} words/min")
        print(f"   Transcript Updates:        {metrics.get('transcript_updates', 0)}")
        print(f"   Final Segments:            {metrics.get('final_segments', 0)}")
        
        # Response Stats
        print("\n🗣️  LLM RESPONSE:")
        print(f"   Output Words:              {metrics.get('output_word_count', 0)}")
        print(f"   Output Characters:         {metrics.get('output_char_count', 0)}")
        print(f"   TTS Chunks Generated:      {metrics.get('llm_chunks_generated', 0)}")
        
        # Network
        print("\n🌐 NETWORK:")
        print(f"   Audio Chunks Sent:         {metrics.get('audio_chunks_sent', 0)}")
        audio_sent_kb = metrics.get('audio_bytes_sent', 0) / 1024
        print(f"   Audio Data Sent:           {audio_sent_kb:.1f} KB")
        
        print("\n" + "=" * 60)


# ============================================================================
# CARTESIA STT CLIENT (Server-side)
# ============================================================================

class CartesiaSTTClient:
    """Cartesia STT WebSocket client for server-side processing."""
    
    def __init__(self, on_transcript=None, on_final=None, stats: SessionStatistics = None):
        self.ws = None
        self.is_connected = False
        self.full_transcript = ""
        self.current_partial = ""
        self.session_done = False
        self.on_transcript = on_transcript  # callback for transcript updates
        self.on_final = on_final  # callback when final transcript ready
        self.stats = stats  # Reference to session stats
        
        # VAD state
        self.speech_started = False
        self.last_speech_time = None
        self.vad_triggered = False
        self.silence_threshold = VAD_THRESHOLD
        
    def build_url(self) -> str:
        query_string = urlencode(CARTESIA_PARAMS)
        return f"{CARTESIA_STT_URL}?{query_string}"
    
    async def connect(self) -> None:
        url = self.build_url()
        try:
            self.ws = await websockets.connect(url, ping_interval=20, ping_timeout=10, close_timeout=5)
            self.is_connected = True
            if self.stats:
                self.stats.timestamps['stt_connected'] = time.time()
        except Exception as e:
            print(f"[STT] Connection failed: {e}")
            raise
    
    def calculate_rms(self, audio_data: bytes) -> float:
        """Calculate RMS energy of audio chunk."""
        count = len(audio_data) // 2
        if count == 0:
            return 0
        shorts = struct.unpack(f"{count}h", audio_data)
        sum_squares = sum(s ** 2 for s in shorts)
        return (sum_squares / count) ** 0.5
    
    def check_voice_activity(self, audio_data: bytes) -> bool:
        """Check if audio chunk contains speech."""
        rms = self.calculate_rms(audio_data)
        is_speech = rms > self.silence_threshold
        current_time = time.time()
        
        if is_speech:
            self.last_speech_time = current_time
            if not self.speech_started:
                self.speech_started = True
                if self.stats:
                    self.stats.timestamps['speech_start'] = current_time
            return True
        else:
            if self.speech_started and self.last_speech_time:
                silence_duration = current_time - self.last_speech_time
                if silence_duration >= SILENCE_DURATION and not self.vad_triggered:
                    self.vad_triggered = True
                    if self.stats:
                        self.stats.timestamps['speech_end'] = self.last_speech_time
                    return False
            return False
    
    async def send_audio(self, audio_data: bytes) -> None:
        """Send audio chunk to Cartesia."""
        if self.ws and self.is_connected and not self.session_done:
            try:
                await self.ws.send(audio_data)
                if self.stats:
                    self.stats.metrics['audio_chunks_sent'] += 1
                    self.stats.metrics['audio_bytes_sent'] += len(audio_data)
            except websockets.exceptions.ConnectionClosed:
                self.is_connected = False
    
    async def send_finalize(self) -> None:
        """Send finalize message to flush remaining audio."""
        if self.ws and self.is_connected:
            try:
                await self.ws.send("finalize")
            except websockets.exceptions.ConnectionClosed:
                pass
    
    async def send_done(self) -> None:
        """Send done message to close session."""
        if self.ws and self.is_connected:
            try:
                await self.ws.send("done")
            except websockets.exceptions.ConnectionClosed:
                pass
    
    async def receive_messages(self) -> None:
        """Receive and process messages from Cartesia."""
        try:
            async for message in self.ws:
                if isinstance(message, str):
                    await self.process_message(message)
                    if self.session_done:
                        break
        except websockets.exceptions.ConnectionClosed:
            self.is_connected = False
    
    async def process_message(self, message: str) -> None:
        """Process a JSON message from Cartesia."""
        try:
            data = json.loads(message)
            msg_type = data.get("type", "")
            
            if msg_type == "transcript":
                text = data.get("text", "")
                is_final = data.get("is_final", False)
                
                if self.stats:
                    self.stats.metrics['transcript_updates'] += 1
                    if not self.stats.timestamps.get('first_transcript'):
                        self.stats.timestamps['first_transcript'] = time.time()
                
                if is_final:
                    if self.full_transcript and not self.full_transcript.endswith(" "):
                        self.full_transcript += " "
                    self.full_transcript += text
                    self.current_partial = ""
                    if self.stats:
                        self.stats.metrics['final_segments'] += 1
                else:
                    self.current_partial = text
                
                # Callback with transcript update
                if self.on_transcript:
                    await self.on_transcript(
                        self.full_transcript + self.current_partial,
                        is_final
                    )
            
            elif msg_type == "done":
                self.session_done = True
                if self.stats:
                    self.stats.timestamps['final_transcript'] = time.time()
                    self.stats.transcript = self.full_transcript.strip()
                if self.on_final:
                    await self.on_final(self.full_transcript.strip())
            
            elif msg_type == "error":
                print(f"[STT] Error: {data.get('message', 'Unknown')}")
                self.session_done = True
                
        except json.JSONDecodeError:
            pass
    
    async def close(self) -> None:
        """Close the WebSocket connection."""
        if self.ws:
            await self.ws.close()
            self.is_connected = False


# ============================================================================
# CARTESIA TTS CLIENT (Server-side)
# ============================================================================

class CartesiaTTSClient:
    """Cartesia TTS WebSocket client for server-side processing."""
    
    def __init__(self, on_audio=None, stats: SessionStatistics = None):
        self.ws = None
        self.is_connected = False
        self.context_id = "jarvis-response"
        self.on_audio = on_audio  # callback for audio chunks
        self.total_audio_bytes = 0
        self.first_audio_time = None
        self.stats = stats  # Reference to session stats
        
    def build_url(self) -> str:
        params = {
            "api_key": CARTESIA_API_KEY,
            "cartesia_version": "2024-11-13",
        }
        return f"{CARTESIA_TTS_URL}?{urlencode(params)}"
    
    async def connect(self) -> None:
        url = self.build_url()
        try:
            self.ws = await websockets.connect(url, ping_interval=20, ping_timeout=10, close_timeout=5)
            self.is_connected = True
            if self.stats:
                self.stats.timestamps['tts_connected'] = time.time()
        except Exception as e:
            print(f"[TTS] Connection failed: {e}")
            raise
    
    async def send_text_chunk(self, text: str, is_last: bool = False) -> None:
        """Send a text chunk for TTS conversion."""
        if not self.ws or not self.is_connected:
            return
        
        if self.stats and not self.stats.timestamps.get('first_tts_chunk_sent'):
            self.stats.timestamps['first_tts_chunk_sent'] = time.time()
        
        request = {
            "model_id": TTS_MODEL,
            "transcript": text,
            "voice": {"mode": "id", "id": TTS_VOICE_ID},
            "output_format": {
                "container": "raw",
                "encoding": "pcm_s16le",
                "sample_rate": OUTPUT_SAMPLE_RATE
            },
            "language": "en",
            "context_id": self.context_id,
            "continue": not is_last,
            "generation_config": {
                "speed": TTS_SPEED,
                "volume": TTS_VOLUME,
                "emotion": TTS_EMOTION
            }
        }
        
        try:
            await self.ws.send(json.dumps(request))
        except websockets.exceptions.ConnectionClosed:
            self.is_connected = False
    
    async def receive_audio(self) -> None:
        """Receive audio chunks and forward via callback."""
        if not self.ws:
            return
        
        try:
            async for message in self.ws:
                if isinstance(message, str):
                    data = json.loads(message)
                    msg_type = data.get("type", "")
                    
                    if msg_type == "chunk":
                        audio_b64 = data.get("data", "")
                        if audio_b64:
                            audio_bytes = base64.b64decode(audio_b64)
                            
                            if not self.first_audio_time:
                                self.first_audio_time = time.time()
                                if self.stats:
                                    self.stats.timestamps['first_audio_response'] = self.first_audio_time
                            
                            self.total_audio_bytes += len(audio_bytes)
                            
                            if self.stats:
                                self.stats.metrics['audio_chunks_received'] += 1
                                self.stats.metrics['audio_bytes_received'] += len(audio_bytes)
                                self.stats.timestamps['last_audio_response'] = time.time()
                            
                            if self.on_audio:
                                await self.on_audio(audio_bytes, data.get("done", False))
                        
                        if data.get("done", False):
                            break
                    
                    elif msg_type == "done":
                        break
                    
                    elif msg_type == "error":
                        print(f"[TTS] Error: {data.get('message', 'Unknown')}")
                        break
                        
        except websockets.exceptions.ConnectionClosed:
            self.is_connected = False
    
    async def close(self) -> None:
        """Close connection."""
        if self.ws:
            await self.ws.close()
        self.is_connected = False


# ============================================================================
# VOICE SESSION HANDLER
# ============================================================================

class VoiceSession:
    """Handles a single voice session with a client."""
    
    def __init__(self, client_ws: WebSocketServerProtocol, llm_provider: str = "cerebras"):
        self.client_ws = client_ws
        self.session_id = str(uuid.uuid4())[:8]
        self.llm_provider = llm_provider
        
        self.stt_client: Optional[CartesiaSTTClient] = None
        self.llm_client: Optional[BaseLLMClient] = None
        self.tts_client: Optional[CartesiaTTSClient] = None
        
        self.audio_queue: asyncio.Queue = asyncio.Queue()
        self.is_active = False
        self.full_transcript = ""
        self.full_response = ""
        
        # Events
        self.stt_done_event = asyncio.Event()
        self.processing_complete = asyncio.Event()
        
        # Initialize statistics
        self.stats = SessionStatistics(
            session_id=self.session_id,
            llm_provider=llm_provider,
            llm_model=CEREBRAS_MODEL if llm_provider == "cerebras" else GEMINI_MODEL
        )
    
    async def send_to_client(self, message: dict) -> None:
        """Send JSON message to client."""
        try:
            await self.client_ws.send(json.dumps(message))
        except websockets.exceptions.ConnectionClosed:
            self.is_active = False
    
    async def send_audio_to_client(self, audio_bytes: bytes, is_final: bool = False) -> None:
        """Send audio chunk to client."""
        # Send as raw binary for efficiency
        try:
            await self.client_ws.send(audio_bytes)
        except websockets.exceptions.ConnectionClosed:
            self.is_active = False
    
    async def on_transcript_update(self, text: str, is_final: bool) -> None:
        """Callback for transcript updates - forward to client."""
        msg = create_message(MessageType.TRANSCRIPT, text=text, is_final=is_final)
        await self.send_to_client(msg)
    
    async def on_final_transcript(self, text: str) -> None:
        """Callback when final transcript is ready."""
        self.full_transcript = text
        self.stt_done_event.set()
    
    async def on_tts_audio(self, audio_bytes: bytes, is_done: bool) -> None:
        """Callback for TTS audio chunks - forward to client."""
        await self.send_audio_to_client(audio_bytes, is_final=is_done)
    
    async def start(self, client_type: str = "unknown") -> None:
        """Initialize and start the session."""
        self.is_active = True
        self.stats.timestamps['session_start'] = time.time()
        self.stats.client_type = client_type
        
        # Create STT client with callbacks and stats
        self.stt_client = CartesiaSTTClient(
            on_transcript=self.on_transcript_update,
            on_final=self.on_final_transcript,
            stats=self.stats
        )
        
        # Create LLM client
        self.llm_client = create_llm_client(self.llm_provider)
        
        # Create TTS client with callback and stats
        self.tts_client = CartesiaTTSClient(
            on_audio=self.on_tts_audio,
            stats=self.stats
        )
        
        # Connect to services
        await self.stt_client.connect()
        
        # Pre-connect TTS for lower latency
        tts_connect_task = asyncio.create_task(self.tts_client.connect())
        
        # Notify client
        await self.send_to_client(create_message(
            MessageType.SESSION_STARTED,
            session_id=self.session_id
        ))
        await self.send_to_client(create_message(
            MessageType.SESSION_READY,
            message="Ready for audio"
        ))
        
        print(f"[{self.session_id}] Session started, waiting for audio...")
        
        # Start STT receiver task
        stt_receiver_task = asyncio.create_task(self.stt_client.receive_messages())
        
        # Process audio from queue
        await self.process_audio_stream()
        
        # Wait for STT to complete
        await self.stt_done_event.wait()
        
        # Ensure TTS is connected
        await tts_connect_task
        
        # Process through LLM + TTS if we have a transcript
        if self.full_transcript:
            print(f"[{self.session_id}] Transcript: {self.full_transcript}")
            
            await self.send_to_client(create_message(
                MessageType.PROCESSING,
                stage="llm",
                message="Generating response..."
            ))
            
            await self.llm_to_tts_pipeline()
            
            # Finalize stats
            self.stats.timestamps['session_end'] = time.time()
            self.stats.response = self.full_response.strip()
            
            # Display and save stats
            self.stats.display()
            self.stats.save_to_file()
            
            # Send done message with stats summary
            done_msg = create_message(
                MessageType.DONE,
                transcript=self.full_transcript,
                response=self.full_response.strip(),
                stats=self.stats.to_dict()
            )
            await self.send_to_client(done_msg)
            
            # Send detailed stats message
            await self.send_to_client(self.stats.to_message())
        else:
            self.stats.timestamps['session_end'] = time.time()
            await self.send_to_client(create_message(
                MessageType.DONE,
                transcript="",
                response=""
            ))
        
        # Cleanup
        await self.cleanup()
    
    async def process_audio_stream(self) -> None:
        """Process incoming audio chunks."""
        while self.is_active and not self.stt_done_event.is_set():
            try:
                # Wait for audio with timeout
                audio_chunk = await asyncio.wait_for(
                    self.audio_queue.get(),
                    timeout=0.1
                )
                
                if audio_chunk is None:
                    # End of audio stream
                    break
                
                # Track first audio received
                if not self.stats.timestamps.get('first_audio_received'):
                    self.stats.timestamps['first_audio_received'] = time.time()
                
                # Check VAD
                self.stt_client.check_voice_activity(audio_chunk)
                
                # Send to STT
                await self.stt_client.send_audio(audio_chunk)
                
                # If VAD triggered end of speech
                if self.stt_client.vad_triggered and not self.stt_client.session_done:
                    print(f"[{self.session_id}] Silence detected, finalizing...")
                    await self.stt_client.send_finalize()
                    await self.stt_client.send_done()
                    break
                    
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                print(f"[{self.session_id}] Audio processing error: {e}")
                break
    
    async def receive_audio(self, audio_data: bytes) -> None:
        """Queue incoming audio chunk for processing."""
        if self.is_active:
            await self.audio_queue.put(audio_data)
    
    async def end_of_speech(self) -> None:
        """Client signals end of speech."""
        if self.stt_client and not self.stt_client.session_done:
            print(f"[{self.session_id}] Client signaled end of speech")
            # Set speech_end if not already set by VAD
            if not self.stats.timestamps.get('speech_end'):
                self.stats.timestamps['speech_end'] = time.time()
            await self.stt_client.send_finalize()
            await self.stt_client.send_done()
    
    async def llm_to_tts_pipeline(self) -> None:
        """Run LLM and TTS in parallel - same as main.py."""
        tts_queue: asyncio.Queue = asyncio.Queue()
        self.stats.timestamps['llm_request_sent'] = time.time()
        
        async def llm_producer():
            chunk_num = 0
            first_token_recorded = False
            first_chunk_recorded = False
            
            try:
                async for chunk in self.llm_client.generate_streaming_response(self.full_transcript):
                    current_time = time.time()
                    
                    # Track first token (approximation - first yield)
                    if not first_token_recorded:
                        self.stats.timestamps['first_llm_token'] = current_time
                        first_token_recorded = True
                    
                    chunk_num += 1
                    self.full_response += chunk + " "
                    
                    # Track chunk times
                    self.stats.metrics['llm_chunk_times'].append(current_time)
                    self.stats.metrics['llm_chunks_generated'] = chunk_num
                    
                    if not first_chunk_recorded:
                        self.stats.timestamps['first_llm_chunk'] = current_time
                        first_chunk_recorded = True
                    
                    # Send text chunk to client for display
                    await self.send_to_client(create_message(
                        MessageType.RESPONSE_TEXT,
                        text=chunk,
                        chunk_index=chunk_num
                    ))
                    
                    await tts_queue.put((chunk, False))
                
                await tts_queue.put((None, True))
                self.stats.timestamps['llm_complete'] = time.time()
                
            except Exception as e:
                print(f"[{self.session_id}] LLM error: {e}")
                await tts_queue.put((None, True))
        
        async def tts_consumer():
            while True:
                text, is_last = await tts_queue.get()
                if text is None:
                    break
                await self.tts_client.send_text_chunk(text, is_last=is_last)
        
        async def tts_receiver():
            await self.tts_client.receive_audio()
        
        await asyncio.gather(llm_producer(), tts_consumer(), tts_receiver())
    
    async def cancel(self) -> None:
        """Cancel the current session."""
        self.is_active = False
        await self.audio_queue.put(None)
        await self.cleanup()
    
    async def cleanup(self) -> None:
        """Cleanup session resources."""
        self.is_active = False
        
        if self.stt_client:
            await self.stt_client.close()
        if self.tts_client:
            await self.tts_client.close()
        
        print(f"[{self.session_id}] Session ended")


# ============================================================================
# WEBSOCKET SERVER
# ============================================================================

class JarvisServer:
    """WebSocket server for Jarvis voice assistant."""
    
    def __init__(self, host: str = "0.0.0.0", port: int = 8080, 
                 llm_provider: str = "cerebras", auth_token: str = None):
        self.host = host
        self.port = port
        self.llm_provider = llm_provider
        self.auth_token = auth_token or AUTH_TOKEN
        self.active_sessions: dict[str, VoiceSession] = {}
    
    def validate_token(self, path: str) -> bool:
        """Validate authentication token from URL query string."""
        try:
            # Parse query parameters from path
            if '?' in path:
                query_string = path.split('?', 1)[1]
                params = parse_qs(query_string)
                token = params.get('token', [None])[0]
                return token == self.auth_token
            return False
        except Exception:
            return False
    
    async def process_request(self, connection, request):
        """Process WebSocket handshake - validate authentication."""
        path = request.path  # Extract path from request object
        if not self.validate_token(path):
            print(f"[Server] Authentication failed for path: {path}")
            return Response(401, "Unauthorized", b"Unauthorized: Invalid or missing token")
        return None  # Allow connection
    
    async def handle_client(self, websocket: WebSocketServerProtocol) -> None:
        """Handle a single client connection."""
        client_id = f"{websocket.remote_address[0]}:{websocket.remote_address[1]}"
        print(f"[Server] New authenticated connection from {client_id}")
        
        session: Optional[VoiceSession] = None
        
        try:
            async for message in websocket:
                # Handle binary audio data
                if isinstance(message, bytes):
                    if session and session.is_active:
                        await session.receive_audio(message)
                    continue
                
                # Handle JSON messages
                try:
                    data = parse_message(message)
                    msg_type = data.get("type", "")
                    
                    if msg_type == MessageType.SESSION_START.value:
                        # Start new session
                        if session:
                            await session.cancel()
                        
                        session = VoiceSession(websocket, self.llm_provider)
                        self.active_sessions[session.session_id] = session
                        
                        # Get client type from message
                        client_type = data.get("client_type", "unknown")
                        
                        # Run session in background
                        asyncio.create_task(session.start(client_type=client_type))
                    
                    elif msg_type == MessageType.AUDIO.value:
                        # Base64 encoded audio
                        if session and session.is_active:
                            audio_bytes = base64.b64decode(data.get("data", ""))
                            await session.receive_audio(audio_bytes)
                    
                    elif msg_type == MessageType.END_OF_SPEECH.value:
                        if session:
                            await session.end_of_speech()
                    
                    elif msg_type == MessageType.CANCEL.value:
                        if session:
                            await session.cancel()
                            session = None
                    
                    elif msg_type == MessageType.PING.value:
                        await websocket.send(json.dumps({"type": MessageType.PONG.value}))
                    
                except json.JSONDecodeError:
                    # Might be raw binary that looks like string
                    if session and session.is_active:
                        await session.receive_audio(message.encode() if isinstance(message, str) else message)
        
        except websockets.exceptions.ConnectionClosed:
            print(f"[Server] Connection closed: {client_id}")
        except Exception as e:
            print(f"[Server] Error handling client {client_id}: {e}")
            import traceback
            traceback.print_exc()
        finally:
            if session:
                await session.cancel()
                if session.session_id in self.active_sessions:
                    del self.active_sessions[session.session_id]
            print(f"[Server] Client disconnected: {client_id}")
    
    async def start(self) -> None:
        """Start the WebSocket server."""
        print("\n" + "=" * 60)
        print("  JARVIS VOICE ASSISTANT SERVER")
        print("=" * 60)
        print(f"\n  Host: {self.host}")
        print(f"  Port: {self.port}")
        print(f"  LLM Provider: {self.llm_provider}")
        print(f"  Auth Token: {self.auth_token[:4]}...{self.auth_token[-4:]}")
        print(f"\n  WebSocket URL: ws://{self.host}:{self.port}?token=<your_token>")
        print(f"\n  Stats File: {STATS_FILE}")
        print("\n" + "=" * 60)
        print("\nWaiting for connections...\n")
        
        async with websockets.serve(
            self.handle_client,
            self.host,
            self.port,
            ping_interval=30,
            ping_timeout=10,
            process_request=self.process_request,
        ):
            await asyncio.Future()  # Run forever


# ============================================================================
# MAIN
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="Jarvis Voice Assistant Server")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8080, help="Port to bind to (default: 8080)")
    parser.add_argument("--llm", default="cerebras", choices=["gemini", "cerebras"],
                        help="LLM provider (default: cerebras)")
    parser.add_argument("--token", default=None, 
                        help="Authentication token (default: from JARVIS_AUTH_TOKEN env or 'jarvis_secret_2024')")
    
    args = parser.parse_args()
    
    server = JarvisServer(
        host=args.host, 
        port=args.port, 
        llm_provider=args.llm,
        auth_token=args.token
    )
    
    try:
        asyncio.run(server.start())
    except KeyboardInterrupt:
        print("\n\n[Server] Shutting down...")


if __name__ == "__main__":
    main()
