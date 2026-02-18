"""
Voice Assistant Module

Handles the voice assistant pipeline:
- STT (Speech-to-Text) via Cartesia
- LLM (Language Model) processing
- TTS (Text-to-Speech) via Cartesia
- Session management and statistics
"""

import asyncio
import json
import time
import struct
import base64
import uuid
from typing import Optional
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import websockets
from websockets.exceptions import ConnectionClosed

from llm import BaseLLMClient
from protocol import MessageType, create_message, OUTPUT_SAMPLE_RATE


# ============================================================================
# CONFIGURATION
# ============================================================================

# Cartesia endpoints
CARTESIA_STT_URL = "wss://api.cartesia.ai/stt/websocket"
CARTESIA_TTS_URL = "wss://api.cartesia.ai/tts/websocket"

# TTS configuration
TTS_VOICE_ID = "5ee9feff-1265-424a-9d7f-8e4d431a12c7"
TTS_MODEL = "sonic-3-2025-10-27"
TTS_SPEED = 1.2
TTS_VOLUME = 1.5
TTS_EMOTION = "neutral"

# VAD settings
SILENCE_DURATION = 1.0
VAD_THRESHOLD = 300

# Statistics storage
STATS_FILE = "session_stats.jsonl"


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
    
    metrics: dict = field(default_factory=lambda: {
        'input_word_count': 0,
        'output_word_count': 0,
        'output_char_count': 0,
        'transcript_updates': 0,
        'final_segments': 0,
        'llm_chunks_generated': 0,
        'llm_tokens': [],
        'llm_chunk_times': [],
        'audio_chunks_sent': 0,
        'audio_bytes_sent': 0,
        'audio_chunks_received': 0,
        'audio_bytes_received': 0,
    })
    
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
        
        speech_duration_ms = ms_diff('speech_end', 'speech_start')
        if speech_duration_ms:
            latencies['speech_duration_ms'] = speech_duration_ms
        
        return {k: v for k, v in latencies.items() if v is not None}
    
    def compute_metrics(self) -> dict:
        """Compute all metrics for the session."""
        m = self.metrics.copy()
        
        if self.transcript:
            m['input_word_count'] = len(self.transcript.split())
        if self.response:
            m['output_word_count'] = len(self.response.split())
            m['output_char_count'] = len(self.response)
        
        speech_duration = None
        if self.timestamps.get('speech_end') and self.timestamps.get('speech_start'):
            speech_duration = self.timestamps['speech_end'] - self.timestamps['speech_start']
            if speech_duration > 0 and m['input_word_count'] > 0:
                m['speaking_rate_wpm'] = int(m['input_word_count'] / speech_duration * 60)
        
        chunk_times = m.get('llm_chunk_times', [])
        if len(chunk_times) > 1:
            intervals = [chunk_times[i] - chunk_times[i-1] for i in range(1, len(chunk_times))]
            m['avg_inter_chunk_ms'] = int(sum(intervals) / len(intervals) * 1000)
        
        m.pop('llm_tokens', None)
        m.pop('llm_chunk_times', None)
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
        
        print("\n⏱️  STT TIMING:")
        if latencies.get('speech_duration_ms'):
            print(f"   Speech Duration:           {latencies['speech_duration_ms']/1000:.2f}s")
        if latencies.get('time_to_first_transcript_ms'):
            print(f"   Time to First Transcript:  {latencies['time_to_first_transcript_ms']}ms")
        if latencies.get('stt_processing_ms'):
            print(f"   STT Processing Latency:    {latencies['stt_processing_ms']}ms")
        if latencies.get('stt_connect_ms'):
            print(f"   WebSocket Connect Time:    {latencies['stt_connect_ms']}ms")
        
        print("\n🤖 LLM TIMING:")
        if latencies.get('time_to_first_llm_token_ms'):
            print(f"   Time to First Token:       {latencies['time_to_first_llm_token_ms']}ms")
        if latencies.get('time_to_first_llm_chunk_ms'):
            print(f"   Time to First Chunk:       {latencies['time_to_first_llm_chunk_ms']}ms")
        if metrics.get('avg_inter_chunk_ms'):
            print(f"   Avg Inter-Chunk Delay:     {metrics['avg_inter_chunk_ms']}ms")
        
        print("\n🔊 TTS TIMING:")
        if latencies.get('tts_latency_ms'):
            print(f"   Time to First Audio:       {latencies['tts_latency_ms']}ms")
        if latencies.get('tts_connect_ms'):
            print(f"   TTS Connect Time:          {latencies['tts_connect_ms']}ms")
        print(f"   Audio Chunks Received:     {metrics.get('audio_chunks_received', 0)}")
        audio_kb = metrics.get('audio_bytes_received', 0) / 1024
        print(f"   Total Audio Output:        {audio_kb:.1f} KB")
        
        print("\n🔄 END-TO-END PIPELINE:")
        if latencies.get('voice_response_latency_ms'):
            print(f"   Voice Response Latency:    {latencies['voice_response_latency_ms']}ms ⭐")
            vrl = latencies['voice_response_latency_ms']
            if vrl < 1500:
                print(f"   Rating:                    ⭐⭐⭐ Excellent (<1.5s)")
            elif vrl < 2500:
                print(f"   Rating:                    ⭐⭐ Good (<2.5s)")
            else:
                print(f"   Rating:                    ⭐ Needs improvement (>2.5s)")
        if latencies.get('total_session_ms'):
            print(f"   Total Session Duration:    {latencies['total_session_ms']/1000:.2f}s")
        
        print("\n📝 TRANSCRIPT:")
        print(f"   Input Words:               {metrics.get('input_word_count', 0)}")
        if metrics.get('speaking_rate_wpm'):
            print(f"   Speaking Rate:             {metrics['speaking_rate_wpm']} words/min")
        print(f"   Transcript Updates:        {metrics.get('transcript_updates', 0)}")
        print(f"   Final Segments:            {metrics.get('final_segments', 0)}")
        
        print("\n🗣️  LLM RESPONSE:")
        print(f"   Output Words:              {metrics.get('output_word_count', 0)}")
        print(f"   Output Characters:         {metrics.get('output_char_count', 0)}")
        print(f"   TTS Chunks Generated:      {metrics.get('llm_chunks_generated', 0)}")
        
        print("\n🌐 NETWORK:")
        print(f"   Audio Chunks Sent:         {metrics.get('audio_chunks_sent', 0)}")
        audio_sent_kb = metrics.get('audio_bytes_sent', 0) / 1024
        print(f"   Audio Data Sent:           {audio_sent_kb:.1f} KB")
        
        print("\n" + "=" * 60)


# ============================================================================
# CARTESIA STT CLIENT
# ============================================================================

class CartesiaSTTClient:
    """Cartesia STT WebSocket client for server-side processing."""
    
    def __init__(self, cartesia_params: dict, on_transcript=None, on_final=None, stats: SessionStatistics = None):
        self.cartesia_params = cartesia_params
        self.ws = None
        self.is_connected = False
        self.full_transcript = ""
        self.current_partial = ""
        self.session_done = False
        self.on_transcript = on_transcript
        self.on_final = on_final
        self.stats = stats
        
        self.speech_started = False
        self.last_speech_time = None
        self.vad_triggered = False
        self.silence_threshold = VAD_THRESHOLD
    
    def build_url(self) -> str:
        from urllib.parse import urlencode
        query_string = urlencode(self.cartesia_params)
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
            except ConnectionClosed:
                self.is_connected = False
    
    async def send_finalize(self) -> None:
        """Send finalize message to flush remaining audio."""
        if self.ws and self.is_connected:
            try:
                await self.ws.send("finalize")
            except ConnectionClosed:
                pass
    
    async def send_done(self) -> None:
        """Send done message to close session."""
        if self.ws and self.is_connected:
            try:
                await self.ws.send("done")
            except ConnectionClosed:
                pass
    
    async def receive_messages(self) -> None:
        """Receive and process messages from Cartesia."""
        try:
            async for message in self.ws:
                if isinstance(message, str):
                    await self.process_message(message)
                    if self.session_done:
                        break
        except ConnectionClosed:
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
# CARTESIA TTS CLIENT
# ============================================================================

class CartesiaTTSClient:
    """Cartesia TTS WebSocket client for server-side processing."""
    
    def __init__(self, cartesia_api_key: str, on_audio=None, stats: SessionStatistics = None):
        self.cartesia_api_key = cartesia_api_key
        self.ws = None
        self.is_connected = False
        self.context_id = "jarvis-response"
        self.on_audio = on_audio
        self.total_audio_bytes = 0
        self.first_audio_time = None
        self.stats = stats
    
    def build_url(self) -> str:
        from urllib.parse import urlencode
        params = {
            "api_key": self.cartesia_api_key,
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
        except ConnectionClosed:
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
                        
        except ConnectionClosed:
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
    
    def __init__(self, client_ws, llm_client: BaseLLMClient, cartesia_api_key: str, 
                 cartesia_params: dict, llm_provider: str, llm_model: str):
        self.client_ws = client_ws
        self.session_id = str(uuid.uuid4())[:8]
        self.llm_provider = llm_provider
        self.llm_model = llm_model
        
        self.stt_client: Optional[CartesiaSTTClient] = None
        self.llm_client = llm_client
        self.tts_client: Optional[CartesiaTTSClient] = None
        self.cartesia_api_key = cartesia_api_key
        self.cartesia_params = cartesia_params
        
        self.audio_queue: asyncio.Queue = asyncio.Queue()
        self.is_active = False
        self.full_transcript = ""
        self.full_response = ""
        
        self.stt_done_event = asyncio.Event()
        self.processing_complete = asyncio.Event()
        
        self.stats = SessionStatistics(
            session_id=self.session_id,
            llm_provider=llm_provider,
            llm_model=llm_model
        )
    
    async def send_to_client(self, message: dict) -> None:
        """Send JSON message to client."""
        try:
            if hasattr(self.client_ws, 'send_json'):
                await self.client_ws.send_json(message)
            else:
                await self.client_ws.send(json.dumps(message))
        except (ConnectionClosed, ConnectionResetError, RuntimeError):
            self.is_active = False
    
    async def send_audio_to_client(self, audio_bytes: bytes, is_final: bool = False) -> None:
        """Send audio chunk to client."""
        try:
            if hasattr(self.client_ws, 'send_bytes'):
                await self.client_ws.send_bytes(audio_bytes)
            else:
                await self.client_ws.send(audio_bytes)
        except (ConnectionClosed, ConnectionResetError, RuntimeError):
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
        
        self.stt_client = CartesiaSTTClient(
            self.cartesia_params,
            on_transcript=self.on_transcript_update,
            on_final=self.on_final_transcript,
            stats=self.stats
        )
        
        self.tts_client = CartesiaTTSClient(
            self.cartesia_api_key,
            on_audio=self.on_tts_audio,
            stats=self.stats
        )
        
        await self.stt_client.connect()
        
        tts_connect_task = asyncio.create_task(self.tts_client.connect())
        
        await self.send_to_client(create_message(
            MessageType.SESSION_STARTED,
            session_id=self.session_id
        ))
        await self.send_to_client(create_message(
            MessageType.SESSION_READY,
            message="Ready for audio"
        ))
        
        print(f"[{self.session_id}] Session started, waiting for audio...")
        
        stt_receiver_task = asyncio.create_task(self.stt_client.receive_messages())
        
        await self.process_audio_stream()
        await self.stt_done_event.wait()
        await tts_connect_task
        
        if self.full_transcript:
            print(f"[{self.session_id}] Transcript: {self.full_transcript}")
            
            await self.send_to_client(create_message(
                MessageType.PROCESSING,
                stage="llm",
                message="Generating response..."
            ))
            
            await self.llm_to_tts_pipeline()
            
            self.stats.timestamps['session_end'] = time.time()
            self.stats.response = self.full_response.strip()
            
            self.stats.display()
            self.stats.save_to_file()
            
            done_msg = create_message(
                MessageType.DONE,
                transcript=self.full_transcript,
                response=self.full_response.strip(),
                stats=self.stats.to_dict()
            )
            await self.send_to_client(done_msg)
            await self.send_to_client(self.stats.to_message())
        else:
            self.stats.timestamps['session_end'] = time.time()
            await self.send_to_client(create_message(
                MessageType.DONE,
                transcript="",
                response=""
            ))
        
        await self.cleanup()
    
    async def process_audio_stream(self) -> None:
        """Process incoming audio chunks."""
        while self.is_active and not self.stt_done_event.is_set():
            try:
                audio_chunk = await asyncio.wait_for(
                    self.audio_queue.get(),
                    timeout=0.1
                )
                
                if audio_chunk is None:
                    break
                
                if not self.stats.timestamps.get('first_audio_received'):
                    self.stats.timestamps['first_audio_received'] = time.time()
                
                self.stt_client.check_voice_activity(audio_chunk)
                await self.stt_client.send_audio(audio_chunk)
                
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
            if not self.stats.timestamps.get('speech_end'):
                self.stats.timestamps['speech_end'] = time.time()
            await self.stt_client.send_finalize()
            await self.stt_client.send_done()
    
    async def llm_to_tts_pipeline(self) -> None:
        """Run LLM and TTS in parallel."""
        tts_queue: asyncio.Queue = asyncio.Queue()
        self.stats.timestamps['llm_request_sent'] = time.time()
        
        async def llm_producer():
            chunk_num = 0
            first_token_recorded = False
            first_chunk_recorded = False
            
            try:
                async for chunk in self.llm_client.generate_streaming_response(self.full_transcript):
                    current_time = time.time()
                    
                    if not first_token_recorded:
                        self.stats.timestamps['first_llm_token'] = current_time
                        first_token_recorded = True
                    
                    chunk_num += 1
                    self.full_response += chunk + " "
                    
                    self.stats.metrics['llm_chunk_times'].append(current_time)
                    self.stats.metrics['llm_chunks_generated'] = chunk_num
                    
                    if not first_chunk_recorded:
                        self.stats.timestamps['first_llm_chunk'] = current_time
                        first_chunk_recorded = True
                    
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
