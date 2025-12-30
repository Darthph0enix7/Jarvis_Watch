"""
Project Jarvis - Phase 1: Speech-to-Text (STT) Layer
Using Cartesia Ink API via WebSockets

This module handles:
- Microphone selection and persistence
- Real-time audio streaming to Cartesia STT
- Live transcription display with proper endpointing
"""

import asyncio
import json
import sys
import os
import time
import struct
import base64
from urllib.parse import urlencode
from typing import AsyncIterator

import pyaudio
import websockets
from google import genai

# ============================================================================
# CONFIGURATION
# ============================================================================

CARTESIA_API_KEY = "sk_car_DAwyAsQnVVDUhqJm6mfBir"  # Replace with your actual API key
CARTESIA_STT_URL = "wss://api.cartesia.ai/stt/websocket"
CARTESIA_TTS_URL = "wss://api.cartesia.ai/tts/websocket"

# TTS configuration
TTS_VOICE_ID = "ffe42012-140d-40ab-8cc3-d3f0e957dbc9"
TTS_MODEL = "sonic-3-2025-10-27"  # Fast model
TTS_SAMPLE_RATE = 24000  # Output sample rate for TTS

# Gemini API configuration
GEMINI_API_KEY = "AIzaSyCg8amdduMspsehvE5UxLdB5lRlmC3Jm64"  # Replace with your Gemini API key
GEMINI_MODEL = "models/gemini-3-flash-preview"  # Fast model for low latency

# Audio settings
SAMPLE_RATE = 16000
CHANNELS = 1
FORMAT = pyaudio.paInt16  # 16-bit PCM
CHUNK_SIZE = 512  # Smaller chunks for faster transcription updates (~32ms at 16kHz)

# VAD (Voice Activity Detection) settings
CALIBRATION_DURATION = 1.0  # Seconds to calibrate ambient noise
VAD_MARGIN = 1.8  # Multiplier above ambient noise for speech detection
SILENCE_DURATION = 1.5  # Seconds of silence before ending session
MIN_SPEECH_DURATION = 0.5  # Minimum speech before VAD can trigger end

# Cartesia API parameters
CARTESIA_PARAMS = {
    "api_key": CARTESIA_API_KEY,
    "cartesia_version": "2024-06-10",
    "model": "ink-whisper",
    "language": "en",
    "encoding": "pcm_s16le",
    "sample_rate": str(SAMPLE_RATE),
    "min_volume": "0.1",
    "max_silence_duration_secs": "2.0",  # CRITICAL: Prevents cutting off on short pauses
}

MIC_CONFIG_FILE = "mic_config.json"

# ============================================================================
# MICROPHONE SETUP
# ============================================================================

def list_input_devices() -> list[dict]:
    """List all available audio input devices."""
    p = pyaudio.PyAudio()
    devices = []
    
    for i in range(p.get_device_count()):
        info = p.get_device_info_by_index(i)
        if info["maxInputChannels"] > 0:  # Only input devices
            devices.append({
                "index": i,
                "name": info["name"],
                "channels": info["maxInputChannels"],
                "sample_rate": int(info["defaultSampleRate"])
            })
    
    p.terminate()
    return devices


def load_mic_config() -> int | None:
    """Load saved microphone device index from config file."""
    if os.path.exists(MIC_CONFIG_FILE):
        try:
            with open(MIC_CONFIG_FILE, "r") as f:
                config = json.load(f)
                return config.get("device_index")
        except (json.JSONDecodeError, IOError):
            return None
    return None


def save_mic_config(device_index: int) -> None:
    """Save microphone device index to config file."""
    with open(MIC_CONFIG_FILE, "w") as f:
        json.dump({"device_index": device_index}, f)
    print(f"✓ Microphone selection saved to {MIC_CONFIG_FILE}")


def select_microphone() -> int:
    """
    Handle microphone selection with persistence.
    Returns the selected device index.
    """
    # Check for saved config
    saved_index = load_mic_config()
    
    if saved_index is not None:
        # Verify the saved device still exists
        devices = list_input_devices()
        device_indices = [d["index"] for d in devices]
        
        if saved_index in device_indices:
            device_name = next(d["name"] for d in devices if d["index"] == saved_index)
            print(f"✓ Using saved microphone: [{saved_index}] {device_name}")
            return saved_index
        else:
            print("⚠ Previously saved microphone not found. Please select a new one.")
    
    # List available devices and prompt user
    devices = list_input_devices()
    
    if not devices:
        print("✗ No input devices found!")
        sys.exit(1)
    
    print("\n" + "=" * 60)
    print("AVAILABLE MICROPHONES")
    print("=" * 60)
    
    for device in devices:
        print(f"  [{device['index']}] {device['name']}")
        print(f"      Channels: {device['channels']}, Sample Rate: {device['sample_rate']} Hz")
    
    print("=" * 60)
    
    while True:
        try:
            selection = input("\nEnter microphone ID: ").strip()
            device_index = int(selection)
            
            if device_index in [d["index"] for d in devices]:
                save_mic_config(device_index)
                return device_index
            else:
                print("✗ Invalid selection. Please choose from the list above.")
        except ValueError:
            print("✗ Please enter a valid number.")


# ============================================================================
# AUDIO CAPTURE
# ============================================================================

class AudioCapture:
    """Handles microphone audio capture with asyncio compatibility."""
    
    def __init__(self, device_index: int):
        self.device_index = device_index
        self.p = pyaudio.PyAudio()
        self.stream = None
        self.is_running = False
    
    def start(self) -> None:
        """Start the audio stream."""
        self.stream = self.p.open(
            format=FORMAT,
            channels=CHANNELS,
            rate=SAMPLE_RATE,
            input=True,
            input_device_index=self.device_index,
            frames_per_buffer=CHUNK_SIZE
        )
        self.is_running = True
        print("✓ Microphone stream started")
    
    def read_chunk(self) -> bytes:
        """Read a chunk of audio data (blocking)."""
        if self.stream and self.is_running:
            return self.stream.read(CHUNK_SIZE, exception_on_overflow=False)
        return b""
    
    def stop(self) -> None:
        """Stop the audio stream."""
        self.is_running = False
        if self.stream:
            self.stream.stop_stream()
            self.stream.close()
        self.p.terminate()
        print("\n✓ Microphone stream stopped")


# ============================================================================
# GEMINI LLM CLIENT
# ============================================================================

class GeminiLLMClient:
    """
    Handles streaming LLM inference with Gemini API.
    Intelligently chunks streaming text for TTS pipeline.
    """
    
    def __init__(self):
        self.client = genai.Client(api_key=GEMINI_API_KEY)
        self.system_instruction = (
            "You are Jarvis, an advanced AI voice assistant. "
            "Respond naturally and concisely. Keep responses conversational and helpful. "
            "Prioritize brevity for voice interactions."
        )
        
        # Chunking configuration
        self.chunk_buffer = ""
        self.min_first_chunk_words = 3  # Very small first chunk for fast response
        self.min_chunk_words = 8  # Subsequent chunks
        self.first_chunk_sent = False
        
        # Punctuation for natural breaks (TTS-friendly)
        self.strong_breaks = {'.', '!', '?', '\n'}  # End of sentence
        self.weak_breaks = {',', ';', ':', '-', '—'}  # Clause boundaries
        
        # Statistics tracking
        self.stats = {
            'request_sent_time': None,
            'first_token_time': None,
            'first_chunk_time': None,
            'response_complete_time': None,
            'total_chunks': 0,
            'total_response_chars': 0,
            'total_response_words': 0,
            'chunk_times': [],  # Time each chunk was yielded
        }
    
    async def generate_streaming_response(
        self,
        prompt: str
    ) -> AsyncIterator[str]:
        """
        Stream LLM response and yield text chunks optimized for TTS.
        First chunk is small for low latency, subsequent chunks respect punctuation.
        """
        self.chunk_buffer = ""
        self.first_chunk_sent = False
        self.stats['chunk_times'] = []
        
        try:
            # Stream response from Gemini
            response = self.client.models.generate_content_stream(
                model=GEMINI_MODEL,
                contents=prompt,
                config={
                    'system_instruction': self.system_instruction,
                    'temperature': 0.7,
                    'max_output_tokens': 512,
                    'top_p': 0.95,
                }
            )
            
            # Process streaming chunks
            first_token_received = False
            for chunk in response:
                if hasattr(chunk, 'text') and chunk.text:
                    # Track first token time
                    if not first_token_received:
                        self.stats['first_token_time'] = time.time()
                        first_token_received = True
                    
                    self.chunk_buffer += chunk.text
                    
                    # Try to yield complete phrases/chunks
                    async for output_chunk in self._process_buffer():
                        self.stats['chunk_times'].append(time.time())
                        if not self.stats['first_chunk_time']:
                            self.stats['first_chunk_time'] = time.time()
                        yield output_chunk
            
            # Flush any remaining text
            if self.chunk_buffer.strip():
                self.stats['chunk_times'].append(time.time())
                yield self.chunk_buffer.strip()
                self.chunk_buffer = ""
                
        except Exception as e:
            print(f"\n✗ Gemini API Error: {e}")
            yield "I apologize, but I encountered an error processing your request."
    
    async def _process_buffer(self) -> AsyncIterator[str]:
        """
        Process buffer and yield complete chunks when ready.
        Implements smart chunking strategy:
        - First chunk: 3+ words, break on any punctuation
        - Later chunks: 8+ words, prefer strong breaks (. ! ?)
        """
        while True:
            chunk = self._extract_chunk()
            if chunk:
                yield chunk
            else:
                break
    
    def _extract_chunk(self) -> str | None:
        """
        Extract a chunk from buffer if conditions are met.
        Returns None if buffer should continue accumulating.
        """
        if not self.chunk_buffer:
            return None
        
        words = self.chunk_buffer.split()
        word_count = len(words)
        
        # Strategy for FIRST chunk: get something out FAST
        if not self.first_chunk_sent:
            if word_count >= self.min_first_chunk_words:
                # Look for ANY break point (weak or strong)
                for i, char in enumerate(self.chunk_buffer):
                    if char in self.strong_breaks or char in self.weak_breaks:
                        # Found a break! Extract up to and including it
                        chunk = self.chunk_buffer[:i+1].strip()
                        if len(chunk.split()) >= self.min_first_chunk_words:
                            self.chunk_buffer = self.chunk_buffer[i+1:]
                            self.first_chunk_sent = True
                            return chunk
                
                # No punctuation yet but have enough words - force first chunk
                if word_count >= self.min_first_chunk_words * 2:
                    chunk = ' '.join(words[:self.min_first_chunk_words])
                    self.chunk_buffer = ' '.join(words[self.min_first_chunk_words:])
                    self.first_chunk_sent = True
                    return chunk
        
        # Strategy for SUBSEQUENT chunks: prefer natural sentence boundaries
        else:
            if word_count >= self.min_chunk_words:
                # Prefer strong breaks (end of sentence)
                for i, char in enumerate(self.chunk_buffer):
                    if char in self.strong_breaks:
                        chunk = self.chunk_buffer[:i+1].strip()
                        if len(chunk.split()) >= self.min_chunk_words:
                            self.chunk_buffer = self.chunk_buffer[i+1:]
                            return chunk
                
                # Fall back to weak breaks (commas, etc.)
                for i, char in enumerate(self.chunk_buffer):
                    if char in self.weak_breaks:
                        chunk = self.chunk_buffer[:i+1].strip()
                        if len(chunk.split()) >= self.min_chunk_words:
                            self.chunk_buffer = self.chunk_buffer[i+1:]
                            return chunk
                
                # Buffer is long but no punctuation - force a chunk
                if word_count >= self.min_chunk_words * 2:
                    chunk = ' '.join(words[:self.min_chunk_words])
                    self.chunk_buffer = ' '.join(words[self.min_chunk_words:])
                    return chunk
        
        return None


# ============================================================================
# CARTESIA TTS CLIENT
# ============================================================================

class CartesiaTTSClient:
    """
    Handles WebSocket connection to Cartesia TTS API.
    Streams text chunks and receives audio in real-time.
    """
    
    def __init__(self):
        self.ws = None
        self.is_connected = False
        self.context_id = "jarvis-response"
        
        # Audio playback
        self.p = pyaudio.PyAudio()
        self.audio_stream = None
        
        # Statistics tracking
        self.stats = {
            'connection_time': None,
            'first_chunk_sent_time': None,
            'first_audio_received_time': None,
            'last_audio_received_time': None,
            'total_chunks_sent': 0,
            'total_audio_chunks_received': 0,
            'total_audio_bytes': 0,
            'chunk_latencies': [],  # Time from text sent to audio received
        }
    
    def build_url(self) -> str:
        """Build WebSocket URL with query parameters."""
        params = {
            "api_key": CARTESIA_API_KEY,
            "cartesia_version": "2024-11-13",
        }
        return f"{CARTESIA_TTS_URL}?{urlencode(params)}"
    
    async def connect(self) -> None:
        """Establish WebSocket connection to Cartesia TTS."""
        url = self.build_url()
        
        try:
            self.ws = await websockets.connect(
                url,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=5
            )
            self.is_connected = True
            self.stats['connection_time'] = time.time()
            
            # Start audio playback stream
            self.audio_stream = self.p.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=TTS_SAMPLE_RATE,
                output=True,
                frames_per_buffer=1024
            )
        except Exception as e:
            print(f"✗ TTS connection failed: {e}")
            raise
    
    async def send_text_chunk(self, text: str, is_last: bool = False) -> None:
        """Send a text chunk for TTS conversion."""
        if not self.ws or not self.is_connected:
            return
        
        if not self.stats['first_chunk_sent_time']:
            self.stats['first_chunk_sent_time'] = time.time()
        
        request = {
            "model_id": TTS_MODEL,
            "transcript": text,
            "voice": {
                "mode": "id",
                "id": TTS_VOICE_ID
            },
            "output_format": {
                "container": "raw",
                "encoding": "pcm_s16le",
                "sample_rate": TTS_SAMPLE_RATE
            },
            "language": "en",
            "context_id": self.context_id,
            "continue": not is_last,
        }
        
        try:
            await self.ws.send(json.dumps(request))
            self.stats['total_chunks_sent'] += 1
        except websockets.exceptions.ConnectionClosed:
            self.is_connected = False
    
    async def receive_and_play_audio(self) -> None:
        """Receive audio chunks and play them in real-time."""
        if not self.ws:
            return
        
        try:
            async for message in self.ws:
                if isinstance(message, str):
                    data = json.loads(message)
                    msg_type = data.get("type", "")
                    
                    if msg_type == "chunk":
                        # Decode and play audio
                        audio_b64 = data.get("data", "")
                        if audio_b64:
                            audio_bytes = base64.b64decode(audio_b64)
                            
                            # Track first audio
                            if not self.stats['first_audio_received_time']:
                                self.stats['first_audio_received_time'] = time.time()
                            
                            self.stats['last_audio_received_time'] = time.time()
                            self.stats['total_audio_chunks_received'] += 1
                            self.stats['total_audio_bytes'] += len(audio_bytes)
                            
                            # Play audio immediately
                            if self.audio_stream:
                                self.audio_stream.write(audio_bytes)
                        
                        # Check if done
                        if data.get("done", False):
                            break
                    
                    elif msg_type == "done":
                        break
                    
                    elif msg_type == "error":
                        print(f"\\n✗ TTS Error: {data.get('message', 'Unknown')}")
                        break
                        
        except websockets.exceptions.ConnectionClosed:
            self.is_connected = False
    
    async def close(self) -> None:
        """Close connection and cleanup."""
        if self.audio_stream:
            self.audio_stream.stop_stream()
            self.audio_stream.close()
        if self.ws:
            await self.ws.close()
        self.p.terminate()
        self.is_connected = False


# ============================================================================
# CARTESIA STT CLIENT
# ============================================================================

class CartesiaSTTClient:
    """
    Handles WebSocket connection to Cartesia STT API.
    Manages audio streaming and transcription reception.
    """
    
    def __init__(self):
        self.ws = None
        self.is_connected = False
        self.full_transcript = ""  # Accumulates final segments
        self.current_partial = ""  # Current partial transcript
        self.session_done = False
        self.vad_triggered = False  # VAD detected end of speech
        self.speech_started = False  # Has speech been detected
        self.last_speech_time = None  # Time of last speech detection
        self.silence_threshold = None  # Adaptive threshold based on calibration
        self.is_calibrated = False  # Has calibration been completed
        
        # Statistics tracking
        self.stats = {
            'session_start_time': None,
            'speech_start_time': None,
            'speech_end_time': None,
            'finalize_sent_time': None,
            'final_transcript_time': None,
            'connection_established_time': None,
            'first_transcript_time': None,
            'total_audio_bytes_sent': 0,
            'total_chunks_sent': 0,
            'transcript_updates': 0,
            'final_segments': 0,
        }
    
    def build_url(self) -> str:
        """Build WebSocket URL with query parameters."""
        query_string = urlencode(CARTESIA_PARAMS)
        return f"{CARTESIA_STT_URL}?{query_string}"
    
    async def connect(self) -> None:
        """Establish WebSocket connection to Cartesia."""
        url = self.build_url()
        print(f"\n🔌 Connecting to Cartesia STT...")
        
        try:
            self.ws = await websockets.connect(
                url,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=5
            )
            self.is_connected = True
            self.stats['connection_established_time'] = time.time()
            print("✓ Connected to Cartesia STT")
            print("✓ Voice Activity Detection enabled (2 sec silence timeout)")
            print("\n" + "=" * 60)
            print("🎤 SPEAK NOW... (Will auto-stop after 2 seconds of silence)")
            print("   Press Ctrl+C to interrupt")
            print("=" * 60 + "\n")
        except Exception as e:
            print(f"✗ Failed to connect: {e}")
            raise
    
    def calculate_rms(self, audio_data: bytes) -> float:
        """Calculate RMS (Root Mean Square) energy of audio chunk."""
        # Convert bytes to shorts (16-bit integers)
        count = len(audio_data) // 2
        shorts = struct.unpack(f"{count}h", audio_data)
        # Calculate RMS
        sum_squares = sum(s ** 2 for s in shorts)
        rms = (sum_squares / count) ** 0.5
        return rms
    
    def check_voice_activity(self, audio_data: bytes) -> bool:
        """Check if audio chunk contains speech (VAD)."""
        rms = self.calculate_rms(audio_data)
        
        # Use calibrated threshold if available, otherwise use default
        threshold = self.silence_threshold if self.silence_threshold else 300
        is_speech = rms > threshold
        
        current_time = time.time()
        
        if is_speech:
            self.last_speech_time = current_time
            if not self.speech_started:
                # First speech detected
                self.speech_started = True
                self.stats['speech_start_time'] = current_time
            return True
        else:
            # Check if we've been silent long enough
            if self.speech_started and self.last_speech_time:
                silence_duration = current_time - self.last_speech_time
                if silence_duration >= SILENCE_DURATION and not self.vad_triggered:
                    self.vad_triggered = True
                    self.stats['speech_end_time'] = self.last_speech_time
                    return False
            return False
    
    async def send_audio(self, audio_data: bytes) -> None:
        """Send audio chunk to Cartesia."""
        if self.ws and self.is_connected and not self.session_done:
            try:
                await self.ws.send(audio_data)
                # Track statistics
                self.stats['total_audio_bytes_sent'] += len(audio_data)
                self.stats['total_chunks_sent'] += 1
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
    
    def display_partial(self, text: str) -> None:
        """Display partial transcript (updates in place)."""
        self.current_partial = text
        display_text = self.full_transcript + text
        # Clear line and print partial (stays on same line)
        sys.stdout.write(f"\r\033[K📝 {display_text}")
        sys.stdout.flush()
    
    def display_final(self, text: str) -> None:
        """Handle final transcript segment."""
        if self.full_transcript and not self.full_transcript.endswith(" "):
            self.full_transcript += " "
        self.full_transcript += text
        self.current_partial = ""
        sys.stdout.write(f"\r\033[K📝 {self.full_transcript}")
        sys.stdout.flush()
    
    def display_done(self) -> None:
        """Display final complete transcription."""
        print()  # New line after partial updates
        print("\n" + "=" * 60)
        print("✅ TRANSCRIPTION COMPLETE")
        print("=" * 60)
        if self.full_transcript.strip():
            print(f"\n{self.full_transcript.strip()}\n")
        else:
            print("\n(No speech detected)\n")
        print("=" * 60)
    
    async def receive_messages(self) -> None:
        """Receive and process messages from Cartesia."""
        try:
            async for message in self.ws:
                if isinstance(message, str):
                    await self.process_message(message)
                    if self.session_done:
                        break
        except websockets.exceptions.ConnectionClosed as e:
            if not self.session_done:
                print(f"\n⚠ Connection closed unexpectedly: {e}")
            self.is_connected = False
    
    async def process_message(self, message: str) -> None:
        """Process a JSON message from Cartesia."""
        try:
            data = json.loads(message)
            msg_type = data.get("type", "")
            
            if msg_type == "transcript":
                text = data.get("text", "")
                is_final = data.get("is_final", False)
                
                # Track first transcript received
                if not self.stats['first_transcript_time']:
                    self.stats['first_transcript_time'] = time.time()
                
                if is_final:
                    self.stats['final_segments'] += 1
                    self.stats['final_transcript_time'] = time.time()
                    self.display_final(text)
                else:
                    self.stats['transcript_updates'] += 1
                    self.display_partial(text)
            
            elif msg_type == "done":
                # Session ended (2-second silence triggered)
                self.session_done = True
                self.display_done()
            
            elif msg_type == "error":
                error_msg = data.get("message", "Unknown error")
                print(f"\n✗ API Error: {error_msg}")
                self.session_done = True
            
        except json.JSONDecodeError:
            print(f"\n⚠ Invalid JSON received: {message[:100]}")
    
    async def close(self) -> None:
        """Close the WebSocket connection."""
        if self.ws:
            await self.ws.close()
            self.is_connected = False


# ============================================================================
# STATISTICS DISPLAY
# ============================================================================

def display_combined_statistics(
    stt_client: CartesiaSTTClient,
    llm_client: GeminiLLMClient,
    tts_client: CartesiaTTSClient = None
) -> None:
    """Display comprehensive statistics for STT + LLM + TTS pipeline."""
    stt_stats = stt_client.stats
    llm_stats = llm_client.stats
    tts_stats = tts_client.stats if tts_client else {}
    
    print("\n" + "=" * 60)
    print("📊 SESSION STATISTICS")
    print("=" * 60)
    
    # ==================== STT TIMING ====================
    print("\n⏱️  STT TIMING:")
    
    speech_duration = None
    if stt_stats['speech_start_time'] and stt_stats['speech_end_time']:
        speech_duration = stt_stats['speech_end_time'] - stt_stats['speech_start_time']
        print(f"   Speech Duration:           {speech_duration:.2f}s")
    
    if stt_stats['speech_start_time'] and stt_stats['first_transcript_time']:
        ttfr = stt_stats['first_transcript_time'] - stt_stats['speech_start_time']
        print(f"   Time to First Transcript:  {ttfr:.3f}s")
    
    if stt_stats['speech_end_time'] and stt_stats['final_transcript_time']:
        stt_latency = stt_stats['final_transcript_time'] - stt_stats['speech_end_time']
        print(f"   STT Processing Latency:    {stt_latency:.3f}s")
    
    if stt_stats['connection_established_time'] and stt_stats['session_start_time']:
        conn_latency = stt_stats['connection_established_time'] - stt_stats['session_start_time']
        print(f"   WebSocket Connect Time:    {conn_latency:.3f}s")
    
    # ==================== LLM TIMING ====================
    print("\n🤖 LLM TIMING:")
    
    if llm_stats.get('request_sent_time') and llm_stats.get('first_token_time'):
        ttft = llm_stats['first_token_time'] - llm_stats['request_sent_time']
        print(f"   Time to First Token:       {ttft:.3f}s")
    
    if llm_stats.get('request_sent_time') and llm_stats.get('first_chunk_time'):
        ttfc = llm_stats['first_chunk_time'] - llm_stats['request_sent_time']
        print(f"   Time to First Chunk:       {ttfc:.3f}s")
    
    if llm_stats.get('request_sent_time') and llm_stats.get('response_complete_time'):
        total_llm = llm_stats['response_complete_time'] - llm_stats['request_sent_time']
        print(f"   Total LLM Response Time:   {total_llm:.3f}s")
    
    if llm_stats.get('chunk_times') and len(llm_stats['chunk_times']) > 1:
        chunk_intervals = []
        for i in range(1, len(llm_stats['chunk_times'])):
            interval = llm_stats['chunk_times'][i] - llm_stats['chunk_times'][i-1]
            chunk_intervals.append(interval)
        if chunk_intervals:
            avg_interval = sum(chunk_intervals) / len(chunk_intervals)
            print(f"   Avg Inter-Chunk Delay:     {avg_interval:.3f}s")
    
    # ==================== TTS TIMING ====================
    if tts_stats:
        print("\n🔊 TTS TIMING:")
        
        if tts_stats.get('first_chunk_sent_time') and tts_stats.get('first_audio_received_time'):
            tts_latency = tts_stats['first_audio_received_time'] - tts_stats['first_chunk_sent_time']
            print(f"   Time to First Audio:       {tts_latency:.3f}s")
        
        if tts_stats.get('first_audio_received_time') and tts_stats.get('last_audio_received_time'):
            audio_duration = tts_stats['last_audio_received_time'] - tts_stats['first_audio_received_time']
            print(f"   Audio Stream Duration:     {audio_duration:.2f}s")
        
        print(f"   TTS Chunks Sent:           {tts_stats.get('total_chunks_sent', 0)}")
        print(f"   Audio Chunks Received:     {tts_stats.get('total_audio_chunks_received', 0)}")
        
        audio_kb = tts_stats.get('total_audio_bytes', 0) / 1024
        print(f"   Total Audio Output:        {audio_kb:.1f} KB")
    
    # ==================== END-TO-END ====================
    print("\n🔄 END-TO-END PIPELINE:")
    
    if stt_stats['speech_start_time'] and llm_stats.get('first_chunk_time'):
        e2e_first = llm_stats['first_chunk_time'] - stt_stats['speech_start_time']
        print(f"   Speech → First LLM Chunk:  {e2e_first:.3f}s")
    
    if stt_stats['speech_end_time'] and llm_stats.get('first_chunk_time'):
        silence_to_chunk = llm_stats['first_chunk_time'] - stt_stats['speech_end_time']
        print(f"   Silence → First LLM Chunk: {silence_to_chunk:.3f}s")
    
    # Key metric: silence to first audio (voice assistant responsiveness)
    if tts_stats and stt_stats.get('speech_end_time') and tts_stats.get('first_audio_received_time'):
        voice_latency = tts_stats['first_audio_received_time'] - stt_stats['speech_end_time']
        print(f"   Silence → First Audio:     {voice_latency:.3f}s ⭐")
    
    if stt_stats['session_start_time'] and tts_stats and tts_stats.get('last_audio_received_time'):
        total_session = tts_stats['last_audio_received_time'] - stt_stats['session_start_time']
        print(f"   Total Session Duration:    {total_session:.2f}s")
    elif stt_stats['session_start_time'] and llm_stats.get('response_complete_time'):
        total_session = llm_stats['response_complete_time'] - stt_stats['session_start_time']
        print(f"   Total Session Duration:    {total_session:.2f}s")
    
    # ==================== TRANSCRIPT STATS ====================
    print("\n📝 TRANSCRIPT:")
    words = stt_client.full_transcript.strip().split()
    print(f"   Input Words:               {len(words)}")
    if speech_duration and speech_duration > 0:
        print(f"   Speaking Rate:             {len(words) / speech_duration:.1f} words/sec")
    print(f"   Partial Updates:           {stt_stats['transcript_updates']}")
    print(f"   Final Segments:            {stt_stats['final_segments']}")
    
    # ==================== LLM RESPONSE STATS ====================
    print("\n🗣️  LLM RESPONSE:")
    if llm_stats.get('total_response_words'):
        print(f"   Output Words:              {llm_stats['total_response_words']}")
    if llm_stats.get('total_response_chars'):
        print(f"   Output Characters:         {llm_stats['total_response_chars']}")
    if llm_stats.get('total_chunks'):
        print(f"   TTS Chunks Generated:      {llm_stats['total_chunks']}")
    
    if llm_stats.get('request_sent_time') and llm_stats.get('response_complete_time') and llm_stats.get('total_response_words'):
        gen_time = llm_stats['response_complete_time'] - llm_stats['request_sent_time']
        if gen_time > 0:
            words_per_sec = llm_stats['total_response_words'] / gen_time
            print(f"   Generation Speed:          {words_per_sec:.1f} words/sec")
    
    # ==================== NETWORK ====================
    print("\n🌐 NETWORK:")
    print(f"   STT Audio Chunks Sent:     {stt_stats['total_chunks_sent']}")
    audio_mb = stt_stats['total_audio_bytes_sent'] / (1024 * 1024)
    print(f"   STT Audio Data:            {audio_mb:.2f} MB")
    if speech_duration and speech_duration > 0:
        throughput = audio_mb / speech_duration
        print(f"   Upload Throughput:         {throughput:.2f} MB/s")
    
    # ==================== EFFICIENCY ====================
    print("\n⚡ EFFICIENCY:")
    
    if speech_duration and stt_stats.get('final_transcript_time') and stt_stats.get('speech_end_time'):
        stt_latency = stt_stats['final_transcript_time'] - stt_stats['speech_end_time']
        stt_ratio = (stt_latency / speech_duration) * 100
        print(f"   STT Latency Ratio:         {stt_ratio:.1f}% of speech")
    
    # Voice assistant responsiveness rating
    if tts_stats and stt_stats.get('speech_end_time') and tts_stats.get('first_audio_received_time'):
        responsiveness = tts_stats['first_audio_received_time'] - stt_stats['speech_end_time']
        print(f"   Voice Response Latency:    {responsiveness:.3f}s")
        if responsiveness < 1.5:
            print(f"   Rating:                    ⭐⭐⭐ Excellent (<1.5s)")
        elif responsiveness < 2.5:
            print(f"   Rating:                    ⭐⭐ Good (<2.5s)")
        else:
            print(f"   Rating:                    ⭐ Needs improvement (>2.5s)")
    elif stt_stats.get('speech_end_time') and llm_stats.get('first_chunk_time'):
        responsiveness = llm_stats['first_chunk_time'] - stt_stats['speech_end_time']
        print(f"   Text Response Latency:     {responsiveness:.3f}s")
    
    print("\n" + "=" * 60)


# ============================================================================
# ASYNC TASKS
# ============================================================================

async def calibration_task(
    audio: AudioCapture,
    client: CartesiaSTTClient
) -> None:
    """
    Task: Brief calibration to measure ambient noise level.
    """
    print("🔧 Calibrating... (measuring ambient noise for 1 second)")
    loop = asyncio.get_event_loop()
    
    rms_values = []
    start_time = time.time()
    
    while (time.time() - start_time) < CALIBRATION_DURATION:
        chunk = await loop.run_in_executor(None, audio.read_chunk)
        if chunk:
            rms = client.calculate_rms(chunk)
            rms_values.append(rms)
        await asyncio.sleep(0.01)  # Small delay
    
    # Calculate baseline (average ambient noise)
    if rms_values:
        avg_ambient = sum(rms_values) / len(rms_values)
        # Set threshold with margin above ambient
        client.silence_threshold = max(avg_ambient * VAD_MARGIN, 200)  # Minimum 200
        client.is_calibrated = True
        print(f"✓ Calibration complete (Ambient: {avg_ambient:.0f}, Threshold: {client.silence_threshold:.0f})")
    else:
        # Fallback if no data
        client.silence_threshold = 300
        client.is_calibrated = True
        print("✓ Calibration complete (using default threshold)")


async def audio_capture_task(
    audio: AudioCapture,
    audio_queue: asyncio.Queue,
    stop_event: asyncio.Event
) -> None:
    """
    Task: Continuously capture audio and put chunks in queue.
    Runs in executor to avoid blocking the event loop.
    """
    loop = asyncio.get_event_loop()
    
    while not stop_event.is_set():
        try:
            # Run blocking read in executor
            chunk = await loop.run_in_executor(None, audio.read_chunk)
            if chunk:
                await audio_queue.put(chunk)
        except Exception as e:
            print(f"\n✗ Audio capture error: {e}")
            break
    
    # Signal end of audio
    await audio_queue.put(None)


async def audio_sender_task(
    client: CartesiaSTTClient,
    audio_queue: asyncio.Queue,
    stop_event: asyncio.Event
) -> None:
    """
    Task: Send audio chunks from queue to Cartesia.
    Monitors VAD and triggers finalize on silence detection.
    """
    while not stop_event.is_set() and client.is_connected and not client.session_done:
        try:
            chunk = await asyncio.wait_for(audio_queue.get(), timeout=0.1)
            if chunk is None:
                break
            
            # Check voice activity
            client.check_voice_activity(chunk)
            
            # Send audio to API
            await client.send_audio(chunk)
            
            # If VAD detected end of speech, finalize
            if client.vad_triggered and not client.session_done:
                print("\n\n🔇 Silence detected - finalizing transcription...")
                client.stats['finalize_sent_time'] = time.time()
                await client.send_finalize()
                await asyncio.sleep(0.5)  # Wait for final transcripts
                client.session_done = True
                client.display_done()
                await client.send_done()
                await client.close()
                stop_event.set()
                break
                
        except asyncio.TimeoutError:
            continue
        except Exception as e:
            if not client.session_done:
                print(f"\n✗ Audio sender error: {e}")
            break


async def message_receiver_task(
    client: CartesiaSTTClient,
    stop_event: asyncio.Event
) -> None:
    """
    Task: Receive and process messages from Cartesia.
    Stops when session completes (2 seconds of silence detected).
    """
    await client.receive_messages()
    # Session ended - signal all other tasks to stop
    stop_event.set()


# ============================================================================
# MAIN
# ============================================================================

async def llm_to_tts_pipeline(
    llm_client: GeminiLLMClient,
    tts_client: CartesiaTTSClient,
    transcript: str
) -> None:
    """
    Run LLM generation and TTS conversion in parallel.
    Chunks are sent to TTS as soon as they're generated.
    """
    tts_queue: asyncio.Queue = asyncio.Queue()
    llm_done = asyncio.Event()
    
    async def llm_producer():
        """Generate LLM response and queue chunks for TTS."""
        chunk_num = 0
        full_response = ""
        
        try:
            async for chunk in llm_client.generate_streaming_response(transcript):
                chunk_num += 1
                full_response += chunk + " "
                print(f"[LLM → TTS] Chunk {chunk_num}: {chunk}")
                await tts_queue.put((chunk, False))  # (text, is_last)
            
            # Signal last chunk
            await tts_queue.put((None, True))
            
            llm_client.stats['response_complete_time'] = time.time()
            llm_client.stats['total_chunks'] = chunk_num
            llm_client.stats['total_response_chars'] = len(full_response.strip())
            llm_client.stats['total_response_words'] = len(full_response.strip().split())
            
        except Exception as e:
            print(f"\n✗ LLM error: {e}")
            await tts_queue.put((None, True))
        finally:
            llm_done.set()
    
    async def tts_consumer():
        """Consume chunks from queue and send to TTS."""
        while True:
            text, is_last = await tts_queue.get()
            
            if text is None:
                break
            
            await tts_client.send_text_chunk(text, is_last=is_last)
    
    async def tts_receiver():
        """Receive and play audio from TTS."""
        await tts_client.receive_and_play_audio()
    
    # Run all three tasks in parallel
    await asyncio.gather(
        llm_producer(),
        tts_consumer(),
        tts_receiver()
    )


async def main() -> None:
    """Main entry point for Jarvis STT + LLM + TTS pipeline."""
    print("\n" + "=" * 60)
    print("  PROJECT JARVIS - Full Voice Pipeline")
    print("  STT: Cartesia Ink | LLM: Gemini | TTS: Cartesia")
    print("=" * 60)
    
    # Step 1: Microphone setup
    device_index = select_microphone()
    
    # Initialize components
    audio = AudioCapture(device_index)
    stt_client = CartesiaSTTClient()
    llm_client = GeminiLLMClient()
    tts_client = CartesiaTTSClient()
    
    audio_queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    stop_event = asyncio.Event()
    
    try:
        # Step 2: Start audio capture
        audio.start()
        stt_client.stats['session_start_time'] = time.time()
        
        # Step 3: Calibrate ambient noise
        await calibration_task(audio, stt_client)
        
        # Step 4: Connect to Cartesia STT
        await stt_client.connect()
        
        # Step 5: Run STT pipeline
        await asyncio.gather(
            audio_capture_task(audio, audio_queue, stop_event),
            audio_sender_task(stt_client, audio_queue, stop_event),
            message_receiver_task(stt_client, stop_event)
        )
        
        # STT cleanup
        stop_event.set()
        if audio.is_running:
            audio.stop()
        
        # Step 6: Process through LLM + TTS pipeline
        if stt_client.full_transcript.strip():
            transcript = stt_client.full_transcript.strip()
            
            print("\n" + "=" * 60)
            print(f"🧠 Processing: {transcript}")
            print("=" * 60)
            print("\n🔊 JARVIS SPEAKING:\n")
            
            # Connect to TTS
            await tts_client.connect()
            
            # Track LLM timing
            llm_client.stats['request_sent_time'] = time.time()
            
            try:
                # Run LLM → TTS pipeline in parallel
                await llm_to_tts_pipeline(llm_client, tts_client, transcript)
                
                print("\n" + "=" * 60)
                print("✅ Response complete")
                print("=" * 60)
                
            except Exception as e:
                print(f"\n✗ Pipeline error: {e}")
                import traceback
                traceback.print_exc()
            finally:
                await tts_client.close()
        else:
            print("\n⚠ No transcript to process")
        
    except KeyboardInterrupt:
        print("\n\n⚠ Interrupted by user")
        if stt_client.full_transcript.strip():
            print("\n" + "=" * 60)
            print("PARTIAL TRANSCRIPTION")
            print("=" * 60)
            print(f"\n{stt_client.full_transcript.strip()}\n")
            print("=" * 60)
    except Exception as e:
        print(f"\n✗ Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        # Final cleanup
        if audio.is_running:
            audio.stop()
        
        # Display combined statistics
        if stt_client.full_transcript:
            display_combined_statistics(stt_client, llm_client, tts_client)
        
        print("\n✓ Jarvis session complete")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
