"""
Jarvis - Ultra-Low Latency Watch Assistant
Phase 1: The Streaming Pipeline

Architecture: Queue-Based Async Pipeline
- All components run in parallel
- Data flows like water through pipes
- Never wait for complete results
"""

import asyncio
import json
import struct
import time
import urllib.parse
import random
from typing import AsyncIterator, Optional, Tuple
from dataclasses import dataclass, field
from collections import deque

import websockets
import pyaudio

# =============================================================================
# CONFIGURATION
# =============================================================================

CARTESIA_API_KEY = "sk_car_11j6V1zfTVZzaeaBKaHZhE"  # Replace with your API key

# Audio Configuration
SAMPLE_RATE = 16000
CHANNELS = 1
CHUNK_SIZE = 1600  # 100ms of audio at 16kHz
FORMAT = pyaudio.paInt16
BYTES_PER_SAMPLE = 2

# Microphone device name (set to None for default, or specify device name)
MIC_DEVICE_NAME = "Usb Microphone"  # Will match "Microphone (Usb Microphone)"

# STT Configuration (Cartesia Ink)
STT_MODEL = "ink-whisper"
STT_LANGUAGE = "en"
STT_ENCODING = "pcm_s16le"
STT_MIN_VOLUME = "0.1"
STT_MAX_SILENCE_SECS = "2"  # Endpointing threshold
CARTESIA_VERSION = "2024-06-10"

# Voice Activity Detection
VAD_ENERGY_THRESHOLD = 500  # RMS threshold for voice detection
VAD_SILENCE_TIMEOUT = 2.0   # Seconds of silence to end utterance

# Pipeline Queue Sizes
QUEUE_MAX_SIZE = 100

# Mock delays (simulating real-world latency)
MOCK_LLM_THINK_DELAY_MS = 20
MOCK_LLM_TOKEN_DELAY_MS = 15
MOCK_TTS_CHUNK_DELAY_MS = 10
MOCK_TTS_WORD_BUFFER = 3


# =============================================================================
# STATS COLLECTOR - Latency Tracking
# =============================================================================

@dataclass
class StatsCollector:
    """Tracks timestamps for latency analysis."""
    
    t0_voice_start: Optional[float] = None      # Mic detects voice energy
    t1_first_transcript: Optional[float] = None  # STT yields first partial
    t2_first_token: Optional[float] = None       # LLM yields first token
    t3_first_audio: Optional[float] = None       # Speaker plays first byte
    
    utterance_end: Optional[float] = None
    total_tokens: int = 0
    total_audio_chunks: int = 0
    transcript_partials: int = 0
    
    def reset(self):
        """Reset all stats for new interaction."""
        self.t0_voice_start = None
        self.t1_first_transcript = None
        self.t2_first_token = None
        self.t3_first_audio = None
        self.utterance_end = None
        self.total_tokens = 0
        self.total_audio_chunks = 0
        self.transcript_partials = 0
    
    def mark_voice_start(self):
        if self.t0_voice_start is None:
            self.t0_voice_start = time.perf_counter()
            print(f"[STATS] T0 Voice Start: {self._now_ms():.1f}ms")
    
    def mark_first_transcript(self):
        if self.t1_first_transcript is None:
            self.t1_first_transcript = time.perf_counter()
            print(f"[STATS] T1 First Transcript: {self._now_ms():.1f}ms (Δ{self._delta(self.t0_voice_start):.1f}ms)")
        self.transcript_partials += 1
    
    def mark_first_token(self):
        if self.t2_first_token is None:
            self.t2_first_token = time.perf_counter()
            print(f"[STATS] T2 First Token: {self._now_ms():.1f}ms (Δ{self._delta(self.t1_first_transcript):.1f}ms)")
        self.total_tokens += 1
    
    def mark_first_audio(self):
        if self.t3_first_audio is None:
            self.t3_first_audio = time.perf_counter()
            print(f"[STATS] T3 First Audio: {self._now_ms():.1f}ms (Δ{self._delta(self.t2_first_token):.1f}ms)")
        self.total_audio_chunks += 1
    
    def mark_utterance_end(self):
        self.utterance_end = time.perf_counter()
    
    def _now_ms(self) -> float:
        if self.t0_voice_start:
            return (time.perf_counter() - self.t0_voice_start) * 1000
        return 0
    
    def _delta(self, from_time: Optional[float]) -> float:
        if from_time:
            return (time.perf_counter() - from_time) * 1000
        return 0
    
    def print_report(self):
        """Print formatted latency report."""
        print("\n" + "=" * 60)
        print("           JARVIS LATENCY REPORT")
        print("=" * 60)
        
        if not self.t0_voice_start:
            print("No interaction recorded.")
            return
        
        def fmt(t: Optional[float]) -> str:
            if t is None:
                return "N/A"
            return f"{(t - self.t0_voice_start) * 1000:.1f}ms"
        
        def delta(t1: Optional[float], t2: Optional[float]) -> str:
            if t1 is None or t2 is None:
                return "N/A"
            return f"{(t2 - t1) * 1000:.1f}ms"
        
        print(f"  T0 Voice Detected:     {fmt(self.t0_voice_start)}")
        print(f"  T1 First Transcript:   {fmt(self.t1_first_transcript)}")
        print(f"  T2 First LLM Token:    {fmt(self.t2_first_token)}")
        print(f"  T3 First Audio Out:    {fmt(self.t3_first_audio)}")
        print("-" * 60)
        print(f"  Voice → Transcript:    {delta(self.t0_voice_start, self.t1_first_transcript)}")
        print(f"  Transcript → Token:    {delta(self.t1_first_transcript, self.t2_first_token)}")
        print(f"  Token → Audio:         {delta(self.t2_first_token, self.t3_first_audio)}")
        print("-" * 60)
        print(f"  TOTAL Time-to-First-Audio: {delta(self.t0_voice_start, self.t3_first_audio)}")
        print("-" * 60)
        print(f"  Transcript Partials:   {self.transcript_partials}")
        print(f"  Total Tokens:          {self.total_tokens}")
        print(f"  Total Audio Chunks:    {self.total_audio_chunks}")
        print("=" * 60 + "\n")


# Global stats instance
stats = StatsCollector()


# =============================================================================
# MICROPHONE STREAM
# =============================================================================

class MicStream:
    """Captures audio from microphone and pushes to queue with VAD."""
    
    def __init__(self, audio_queue: asyncio.Queue):
        self.audio_queue = audio_queue
        self.pyaudio_instance = pyaudio.PyAudio()
        self.stream = None
        self.is_running = False
        self.voice_active = False
        self.silence_start: Optional[float] = None
        self.device_index = self._find_device_index()
    
    def _find_device_index(self) -> Optional[int]:
        """Find the microphone device index by name."""
        if MIC_DEVICE_NAME is None:
            return None
        
        for i in range(self.pyaudio_instance.get_device_count()):
            info = self.pyaudio_instance.get_device_info_by_index(i)
            if MIC_DEVICE_NAME.lower() in info['name'].lower() and info['maxInputChannels'] > 0:
                print(f"[MIC] Found device: {info['name']} (index {i})")
                return i
        
        print(f"[MIC] ⚠️ Device '{MIC_DEVICE_NAME}' not found, using default")
        return None
        
    def _calculate_rms(self, audio_data: bytes) -> float:
        """Calculate RMS energy of audio chunk."""
        if len(audio_data) < 2:
            return 0
        
        # Unpack 16-bit samples
        count = len(audio_data) // 2
        samples = struct.unpack(f'<{count}h', audio_data)
        
        # Calculate RMS
        sum_squares = sum(s * s for s in samples)
        rms = (sum_squares / count) ** 0.5
        return rms
    
    async def start(self):
        """Start capturing audio."""
        self.stream = self.pyaudio_instance.open(
            format=FORMAT,
            channels=CHANNELS,
            rate=SAMPLE_RATE,
            input=True,
            input_device_index=self.device_index,
            frames_per_buffer=CHUNK_SIZE
        )
        self.is_running = True
        print("[MIC] 🎤 Microphone started - listening...")
        
    async def capture_loop(self) -> bool:
        """
        Capture audio and push to queue.
        Returns True if utterance completed, False if stopped.
        """
        self.voice_active = False
        self.silence_start = None
        
        while self.is_running:
            try:
                # Read audio chunk (non-blocking via executor)
                audio_data = await asyncio.get_event_loop().run_in_executor(
                    None, 
                    lambda: self.stream.read(CHUNK_SIZE, exception_on_overflow=False)
                )
                
                rms = self._calculate_rms(audio_data)
                
                # Voice Activity Detection
                if rms > VAD_ENERGY_THRESHOLD:
                    if not self.voice_active:
                        self.voice_active = True
                        stats.mark_voice_start()
                        print(f"[MIC] 🗣️ Voice detected (RMS: {rms:.0f})")
                    self.silence_start = None
                    
                    # Push audio to queue
                    await self.audio_queue.put(audio_data)
                    
                elif self.voice_active:
                    # Still send audio during brief silences (for natural speech)
                    await self.audio_queue.put(audio_data)
                    
                    # Track silence duration
                    if self.silence_start is None:
                        self.silence_start = time.perf_counter()
                    elif time.perf_counter() - self.silence_start > VAD_SILENCE_TIMEOUT:
                        print(f"[MIC] 🔇 Silence timeout - utterance complete")
                        stats.mark_utterance_end()
                        return True  # Utterance complete
                
                await asyncio.sleep(0.001)  # Yield to event loop
                
            except Exception as e:
                print(f"[MIC] Error: {e}")
                break
        
        return False
    
    async def stop(self):
        """Stop capturing."""
        self.is_running = False
        if self.stream:
            self.stream.stop_stream()
            self.stream.close()
        self.pyaudio_instance.terminate()
        print("[MIC] Microphone stopped")


# =============================================================================
# CARTESIA STREAMING STT (Real Implementation)
# =============================================================================

class CartesiaStreamingSTT:
    """
    Real-time Speech-to-Text using Cartesia Ink API.
    Streams audio via WebSocket, yields (text, is_final) tuples.
    """
    
    def __init__(self, audio_queue: asyncio.Queue, text_queue: asyncio.Queue):
        self.audio_queue = audio_queue
        self.text_queue = text_queue
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.is_running = False
        self.current_transcript = ""
        
    def _build_url(self) -> str:
        """Build WebSocket URL with all config in query params."""
        params = {
            "api_key": CARTESIA_API_KEY,
            "cartesia_version": CARTESIA_VERSION,
            "model": STT_MODEL,
            "language": STT_LANGUAGE,
            "encoding": STT_ENCODING,
            "sample_rate": str(SAMPLE_RATE),
            "min_volume": STT_MIN_VOLUME,
            "max_silence_duration_secs": STT_MAX_SILENCE_SECS,
        }
        url = f"wss://api.cartesia.ai/stt/websocket?{urllib.parse.urlencode(params)}"
        return url
    
    async def connect(self):
        """Establish WebSocket connection."""
        url = self._build_url()
        print(f"[STT] Connecting to Cartesia Ink...")
        self.ws = await websockets.connect(url)
        self.is_running = True
        print("[STT] ✓ Connected to Cartesia Ink")
    
    async def _send_audio_loop(self):
        """Send audio chunks from queue to WebSocket."""
        try:
            while self.is_running:
                try:
                    # Get audio with timeout to check running status
                    audio_chunk = await asyncio.wait_for(
                        self.audio_queue.get(),
                        timeout=0.1
                    )
                    
                    # Send raw binary audio
                    if self.ws and audio_chunk:
                        await self.ws.send(audio_chunk)
                        
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    print(f"[STT] Send error: {e}")
                    break
                    
        except asyncio.CancelledError:
            pass
    
    async def _receive_transcripts_loop(self):
        """Receive transcripts from WebSocket, push to text queue."""
        accumulated_final = ""
        last_partial = ""
        
        try:
            while self.is_running and self.ws:
                try:
                    msg = await asyncio.wait_for(self.ws.recv(), timeout=0.5)
                    data = json.loads(msg)
                    
                    msg_type = data.get("type", "")
                    
                    # Debug: print raw message
                    print(f"[STT] 📨 Raw: {msg[:200]}..." if len(msg) > 200 else f"[STT] 📨 Raw: {msg}")
                    
                    if msg_type == "transcript":
                        # Try multiple possible field names for the text
                        text = (data.get("transcript") or data.get("text") or "").strip()
                        is_final = data.get("is_final", False)
                        
                        # Get words if available (Cartesia includes word-level timestamps)
                        words = data.get("words", [])
                        if words and not text:
                            text = " ".join(w.get("word", "") for w in words).strip()
                        
                        if text:
                            stats.mark_first_transcript()
                            
                            if is_final:
                                # This is a segment-final (comma pause, etc.)
                                # Accumulate it but DON'T treat as utterance end
                                accumulated_final = f"{accumulated_final} {text}".strip()
                                print(f"[STT] 📝 Segment: \"{text}\" [final segment]")
                                
                                # Push partial to LLM for early processing
                                await self.text_queue.put((text, False))
                                last_partial = ""
                            else:
                                # Partial transcript - show and track
                                if text != last_partial:
                                    print(f"[STT] 📝 Partial: \"{text}\"")
                                    last_partial = text
                    
                    elif msg_type == "flush_done":
                        print("[STT] Flush acknowledged")
                        # If we have accumulated text, push it
                        if accumulated_final:
                            await self.text_queue.put((accumulated_final, True))
                            accumulated_final = ""
                        elif last_partial:
                            # Push the last partial as final
                            await self.text_queue.put((last_partial, True))
                            last_partial = ""
                        
                    elif msg_type == "done":
                        print("[STT] Session done")
                        # Push any remaining accumulated transcript
                        if accumulated_final:
                            await self.text_queue.put((accumulated_final, True))
                        elif last_partial:
                            await self.text_queue.put((last_partial, True))
                        break
                        
                    elif msg_type == "error":
                        error_msg = data.get("message", "Unknown error")
                        print(f"[STT] ❌ Error: {error_msg}")
                        break
                        
                except asyncio.TimeoutError:
                    continue
                except websockets.ConnectionClosed:
                    print("[STT] Connection closed")
                    break
                    
        except asyncio.CancelledError:
            pass
        
        # Signal end of transcription
        await self.text_queue.put(("", True))
    
    async def process(self):
        """Main processing - run send and receive in parallel."""
        send_task = asyncio.create_task(self._send_audio_loop())
        recv_task = asyncio.create_task(self._receive_transcripts_loop())
        
        # Wait for both tasks
        await asyncio.gather(send_task, recv_task, return_exceptions=True)
    
    async def finalize(self):
        """Signal end of audio input."""
        if self.ws:
            try:
                # Send finalize command as text
                await self.ws.send("finalize")
                print("[STT] Sent finalize command")
                
                # Wait briefly for flush_done
                await asyncio.sleep(0.2)
                
                # Send done command
                await self.ws.send("done")
                print("[STT] Sent done command")
            except Exception as e:
                print(f"[STT] Finalize error: {e}")
    
    async def stop(self):
        """Stop STT processing."""
        self.is_running = False
        if self.ws:
            try:
                await self.ws.close()
            except:
                pass
        print("[STT] Stopped")


# =============================================================================
# MOCK STREAMING LLM
# =============================================================================

class MockStreamingLLM:
    """
    Mock LLM that simulates streaming token generation.
    Starts outputting tokens while still receiving input.
    """
    
    MOCK_RESPONSES = [
        "I understand you're asking about that topic Let me help you with some information",
        "That's a great question Here's what I can tell you about it",
        "Sure I'd be happy to assist you with that request",
        "Based on what you've said I think the best approach would be",
        "Let me think about that for a moment Here's my suggestion",
    ]
    
    def __init__(self, text_queue: asyncio.Queue, token_queue: asyncio.Queue):
        self.text_queue = text_queue
        self.token_queue = token_queue
        self.is_running = False
        
    async def process(self):
        """
        Process incoming text and generate streaming tokens.
        Starts generating as soon as meaningful input arrives.
        """
        self.is_running = True
        input_buffer = ""
        generation_started = False
        
        print("[LLM] 🧠 Mock LLM ready")
        
        while self.is_running:
            try:
                # Get text from STT
                text, is_final = await asyncio.wait_for(
                    self.text_queue.get(),
                    timeout=0.1
                )
                
                if text:
                    input_buffer = f"{input_buffer} {text}".strip()
                    print(f"[LLM] 📥 Received: \"{text}\" (buffer: {len(input_buffer)} chars)")
                
                # Start generating if we have enough input OR it's final
                if (len(input_buffer) > 10 or is_final) and not generation_started:
                    generation_started = True
                    
                    # Simulate thinking delay
                    await asyncio.sleep(MOCK_LLM_THINK_DELAY_MS / 1000)
                    
                    # Generate mock response
                    response = random.choice(self.MOCK_RESPONSES)
                    tokens = response.split()
                    
                    print(f"[LLM] 🚀 Starting generation ({len(tokens)} tokens)")
                    
                    # Stream tokens
                    for i, token in enumerate(tokens):
                        stats.mark_first_token()
                        await self.token_queue.put(token)
                        print(f"[LLM] 💬 Token {i+1}: \"{token}\"")
                        await asyncio.sleep(MOCK_LLM_TOKEN_DELAY_MS / 1000)
                    
                    # Signal end of generation
                    await self.token_queue.put(None)
                    print("[LLM] ✓ Generation complete")
                
                if is_final and generation_started:
                    break
                elif is_final and not input_buffer:
                    break
                    
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
        
        # Ensure we signal completion
        await self.token_queue.put(None)
        self.is_running = False
        print("[LLM] Stopped")
    
    async def stop(self):
        self.is_running = False


# =============================================================================
# MOCK STREAMING TTS
# =============================================================================

class MockStreamingTTS:
    """
    Mock TTS that simulates streaming audio synthesis.
    Starts producing audio as soon as enough tokens accumulate.
    """
    
    def __init__(self, token_queue: asyncio.Queue, audio_out_queue: asyncio.Queue):
        self.token_queue = token_queue
        self.audio_out_queue = audio_out_queue
        self.is_running = False
        
    def _generate_audio_chunk(self, word_count: int) -> bytes:
        """Generate dummy audio bytes (silence with slight noise)."""
        # ~100ms of audio per word at 16kHz, 16-bit
        samples_per_word = int(SAMPLE_RATE * 0.1)
        total_samples = samples_per_word * word_count
        
        # Generate low-amplitude noise (sounds like soft static)
        samples = [random.randint(-100, 100) for _ in range(total_samples)]
        audio_data = struct.pack(f'<{total_samples}h', *samples)
        return audio_data
    
    async def process(self):
        """
        Process tokens and generate streaming audio.
        Buffers a few words then starts outputting audio.
        """
        self.is_running = True
        word_buffer = []
        
        print("[TTS] 🔊 Mock TTS ready")
        
        while self.is_running:
            try:
                token = await asyncio.wait_for(
                    self.token_queue.get(),
                    timeout=0.1
                )
                
                if token is None:
                    # End of tokens - flush buffer
                    if word_buffer:
                        audio = self._generate_audio_chunk(len(word_buffer))
                        stats.mark_first_audio()
                        await self.audio_out_queue.put(audio)
                        print(f"[TTS] 🎵 Final chunk: {len(word_buffer)} words")
                        word_buffer = []
                    
                    # Signal end of audio
                    await self.audio_out_queue.put(None)
                    break
                
                word_buffer.append(token)
                print(f"[TTS] 📥 Buffered: \"{token}\" ({len(word_buffer)}/{MOCK_TTS_WORD_BUFFER})")
                
                # Generate audio when buffer is full
                if len(word_buffer) >= MOCK_TTS_WORD_BUFFER:
                    await asyncio.sleep(MOCK_TTS_CHUNK_DELAY_MS / 1000)
                    
                    audio = self._generate_audio_chunk(len(word_buffer))
                    stats.mark_first_audio()
                    await self.audio_out_queue.put(audio)
                    print(f"[TTS] 🎵 Synthesized: \"{' '.join(word_buffer)}\"")
                    word_buffer = []
                    
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
        
        self.is_running = False
        print("[TTS] Stopped")
    
    async def stop(self):
        self.is_running = False


# =============================================================================
# SPEAKER OUTPUT
# =============================================================================

class Speaker:
    """Plays audio chunks immediately as they arrive."""
    
    def __init__(self, audio_queue: asyncio.Queue):
        self.audio_queue = audio_queue
        self.pyaudio_instance = pyaudio.PyAudio()
        self.stream = None
        self.is_running = False
        
    async def start(self):
        """Initialize audio output stream."""
        self.stream = self.pyaudio_instance.open(
            format=FORMAT,
            channels=CHANNELS,
            rate=SAMPLE_RATE,
            output=True,
            frames_per_buffer=CHUNK_SIZE
        )
        self.is_running = True
        print("[SPEAKER] 🔈 Speaker ready")
    
    async def play_loop(self):
        """Play audio chunks as they arrive."""
        chunks_played = 0
        
        while self.is_running:
            try:
                audio_chunk = await asyncio.wait_for(
                    self.audio_queue.get(),
                    timeout=0.1
                )
                
                if audio_chunk is None:
                    print(f"[SPEAKER] ✓ Playback complete ({chunks_played} chunks)")
                    break
                
                # Play audio (via executor to not block)
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: self.stream.write(audio_chunk)
                )
                chunks_played += 1
                print(f"[SPEAKER] ▶️ Playing chunk {chunks_played} ({len(audio_chunk)} bytes)")
                
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
    
    async def stop(self):
        """Stop playback."""
        self.is_running = False
        if self.stream:
            self.stream.stop_stream()
            self.stream.close()
        self.pyaudio_instance.terminate()
        print("[SPEAKER] Stopped")


# =============================================================================
# MAIN ORCHESTRATOR
# =============================================================================

class JarvisPipeline:
    """
    Main orchestrator for the streaming pipeline.
    Coordinates all components running in parallel.
    """
    
    def __init__(self):
        # Create queues
        self.audio_in_queue = asyncio.Queue(maxsize=QUEUE_MAX_SIZE)
        self.text_queue = asyncio.Queue(maxsize=QUEUE_MAX_SIZE)
        self.token_queue = asyncio.Queue(maxsize=QUEUE_MAX_SIZE)
        self.audio_out_queue = asyncio.Queue(maxsize=QUEUE_MAX_SIZE)
        
        # Create components
        self.mic = MicStream(self.audio_in_queue)
        self.stt = CartesiaStreamingSTT(self.audio_in_queue, self.text_queue)
        self.llm = MockStreamingLLM(self.text_queue, self.token_queue)
        self.tts = MockStreamingTTS(self.token_queue, self.audio_out_queue)
        self.speaker = Speaker(self.audio_out_queue)
    
    async def run_single_turn(self):
        """Run a single voice interaction turn."""
        print("\n" + "=" * 60)
        print("  JARVIS - Listening for your command...")
        print("=" * 60 + "\n")
        
        stats.reset()
        
        # Start mic and speaker
        await self.mic.start()
        await self.speaker.start()
        
        # Connect to STT
        await self.stt.connect()
        
        try:
            # Create all tasks
            mic_task = asyncio.create_task(self.mic.capture_loop())
            stt_task = asyncio.create_task(self.stt.process())
            llm_task = asyncio.create_task(self.llm.process())
            tts_task = asyncio.create_task(self.tts.process())
            speaker_task = asyncio.create_task(self.speaker.play_loop())
            
            # Wait for mic to detect end of utterance
            utterance_complete = await mic_task
            
            if utterance_complete:
                print("\n[PIPELINE] 📤 Finalizing STT...")
                await self.stt.finalize()
            
            # Wait for processing to complete with timeout
            try:
                await asyncio.wait_for(
                    asyncio.gather(stt_task, llm_task, tts_task, speaker_task),
                    timeout=30.0
                )
            except asyncio.TimeoutError:
                print("[PIPELINE] ⚠️ Processing timeout")
            
        except Exception as e:
            print(f"[PIPELINE] Error: {e}")
            import traceback
            traceback.print_exc()
            
        finally:
            # Cleanup
            await self.mic.stop()
            await self.stt.stop()
            await self.llm.stop()
            await self.tts.stop()
            await self.speaker.stop()
    
    async def run_interactive(self):
        """Run interactive loop."""
        print("\n" + "=" * 60)
        print("       JARVIS - Ultra-Low Latency Voice Assistant")
        print("              Press Ctrl+C to exit")
        print("=" * 60)
        
        try:
            while True:
                await self.run_single_turn()
                stats.print_report()
                
                # Clear queues for next turn
                self._clear_queues()
                
                # Small delay before next turn
                print("\n[PIPELINE] Ready for next command in 2 seconds...")
                await asyncio.sleep(2)
                
                # Recreate components for next turn
                self.stt = CartesiaStreamingSTT(self.audio_in_queue, self.text_queue)
                self.llm = MockStreamingLLM(self.text_queue, self.token_queue)
                self.tts = MockStreamingTTS(self.token_queue, self.audio_out_queue)
                
        except KeyboardInterrupt:
            print("\n[PIPELINE] Shutting down...")
    
    def _clear_queues(self):
        """Clear all queues."""
        for q in [self.audio_in_queue, self.text_queue, 
                  self.token_queue, self.audio_out_queue]:
            while not q.empty():
                try:
                    q.get_nowait()
                except:
                    pass


# =============================================================================
# ENTRY POINT
# =============================================================================

async def main():
    """Main entry point."""
    # Verify API key
    if CARTESIA_API_KEY == "your-api-key-here":
        print("=" * 60)
        print("  ⚠️  WARNING: Please set your CARTESIA_API_KEY!")
        print("  Edit the configuration at the top of this file.")
        print("=" * 60)
        print("\nRunning in demo mode with simulated STT...\n")
        
        # Run demo with mock STT
        await run_demo_mode()
        return
    
    # Run full pipeline
    pipeline = JarvisPipeline()
    await pipeline.run_interactive()


async def run_demo_mode():
    """Demo mode with all mocks for testing without API key."""
    
    class MockSTT:
        """Mock STT for demo."""
        def __init__(self, text_queue):
            self.text_queue = text_queue
            
        async def simulate(self):
            print("[DEMO STT] Simulating speech input...")
            stats.mark_voice_start()
            await asyncio.sleep(0.5)
            
            # Simulate partials
            partials = ["Hello", "Hello Jarvis", "Hello Jarvis how are"]
            for p in partials:
                stats.mark_first_transcript()
                print(f"[DEMO STT] Partial: \"{p}\"")
                await asyncio.sleep(0.2)
            
            # Final
            final_text = "Hello Jarvis how are you today"
            print(f"[DEMO STT] Final: \"{final_text}\"")
            await self.text_queue.put((final_text, True))
    
    # Create queues
    text_queue = asyncio.Queue()
    token_queue = asyncio.Queue()
    audio_queue = asyncio.Queue()
    
    # Create components
    mock_stt = MockSTT(text_queue)
    llm = MockStreamingLLM(text_queue, token_queue)
    tts = MockStreamingTTS(token_queue, audio_queue)
    
    stats.reset()
    
    print("\n" + "=" * 60)
    print("  DEMO MODE - Testing Pipeline Flow")
    print("=" * 60 + "\n")
    
    # Run all in parallel
    await asyncio.gather(
        mock_stt.simulate(),
        llm.process(),
        tts.process(),
        consume_audio(audio_queue)
    )
    
    stats.print_report()


async def consume_audio(audio_queue):
    """Consume audio chunks (demo mode)."""
    chunks = 0
    while True:
        try:
            chunk = await asyncio.wait_for(audio_queue.get(), timeout=5.0)
            if chunk is None:
                break
            stats.mark_first_audio()
            chunks += 1
            print(f"[DEMO SPEAKER] ▶️ Would play chunk {chunks}")
        except asyncio.TimeoutError:
            break
    print(f"[DEMO SPEAKER] Complete ({chunks} chunks)")


if __name__ == "__main__":
    asyncio.run(main())
