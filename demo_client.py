"""
Jarvis Voice Assistant - Demo Client

A local test client to simulate a watch/phone client on the same machine.
Connects to the Jarvis server and allows voice interaction.

Usage:
    1. Start the server: python server.py
    2. Run this client: python demo_client.py [--server URL]

Controls:
    - Press ENTER to start recording
    - Press ENTER again to stop recording (or wait for auto-stop)
    - Press 'q' + ENTER to quit

Example:
    python demo_client.py --server ws://localhost:8080
"""

import asyncio
import json
import sys
import os
import time
import base64
import struct
import argparse
import threading
from typing import Optional

import pyaudio
import websockets

# Import protocol definitions
from protocol import (
    MessageType, INPUT_SAMPLE_RATE, OUTPUT_SAMPLE_RATE,
    INPUT_CHUNK_SIZE, create_message, parse_message
)

# ============================================================================
# CONFIGURATION
# ============================================================================

DEFAULT_SERVER_URL = "ws://158.180.35.23:8000"
DEFAULT_AUTH_TOKEN = "Denemeler123."

# Audio settings
SAMPLE_RATE = INPUT_SAMPLE_RATE  # 16kHz for input
CHANNELS = 1
FORMAT = pyaudio.paInt16
CHUNK_SIZE = 512  # ~32ms chunks

# Voice Activity Detection settings
SILENCE_THRESHOLD = 500  # Energy threshold for silence
SILENCE_DURATION = 1.0  # Seconds of silence before auto-stop
MIN_SPEECH_DURATION = 0.5  # Minimum seconds of speech before allowing stop

# Mic config file (reuse from main.py)
MIC_CONFIG_FILE = "mic_config.json"


# ============================================================================
# MICROPHONE MANAGEMENT
# ============================================================================

def list_input_devices() -> list[dict]:
    """List all available audio input devices."""
    p = pyaudio.PyAudio()
    devices = []
    
    for i in range(p.get_device_count()):
        info = p.get_device_info_by_index(i)
        if info["maxInputChannels"] > 0:
            devices.append({
                "index": i,
                "name": info["name"],
                "channels": info["maxInputChannels"],
                "sample_rate": int(info["defaultSampleRate"])
            })
    
    p.terminate()
    return devices


def load_mic_config() -> int | None:
    """Load saved microphone device index."""
    if os.path.exists(MIC_CONFIG_FILE):
        try:
            with open(MIC_CONFIG_FILE, "r") as f:
                config = json.load(f)
                return config.get("device_index")
        except (json.JSONDecodeError, IOError):
            return None
    return None


def save_mic_config(device_index: int) -> None:
    """Save microphone device index."""
    with open(MIC_CONFIG_FILE, "w") as f:
        json.dump({"device_index": device_index}, f)


def select_microphone() -> int:
    """Handle microphone selection with persistence."""
    saved_index = load_mic_config()
    
    if saved_index is not None:
        devices = list_input_devices()
        device_indices = [d["index"] for d in devices]
        
        if saved_index in device_indices:
            device_name = next(d["name"] for d in devices if d["index"] == saved_index)
            print(f"✓ Using saved microphone: [{saved_index}] {device_name}")
            return saved_index
    
    devices = list_input_devices()
    
    if not devices:
        print("✗ No input devices found!")
        sys.exit(1)
    
    print("\n" + "=" * 60)
    print("AVAILABLE MICROPHONES")
    print("=" * 60)
    
    for device in devices:
        print(f"  [{device['index']}] {device['name']}")
    
    print("=" * 60)
    
    while True:
        try:
            selection = input("\nEnter microphone ID: ").strip()
            device_index = int(selection)
            
            if device_index in [d["index"] for d in devices]:
                save_mic_config(device_index)
                return device_index
            else:
                print("✗ Invalid selection.")
        except ValueError:
            print("✗ Please enter a valid number.")


# ============================================================================
# AUDIO CAPTURE
# ============================================================================

class AudioCapture:
    """Microphone audio capture with voice activity detection."""
    
    def __init__(self, device_index: int):
        self.device_index = device_index
        self.p = pyaudio.PyAudio()
        self.stream = None
        self.is_running = False
        
        # VAD state
        self.silence_start = None
        self.speech_start = None
        self.has_speech = False
    
    def start(self) -> None:
        self.stream = self.p.open(
            format=FORMAT,
            channels=CHANNELS,
            rate=SAMPLE_RATE,
            input=True,
            input_device_index=self.device_index,
            frames_per_buffer=CHUNK_SIZE
        )
        self.is_running = True
        self.silence_start = None
        self.speech_start = None
        self.has_speech = False
    
    def read_chunk(self) -> bytes:
        if self.stream and self.is_running:
            return self.stream.read(CHUNK_SIZE, exception_on_overflow=False)
        return b""
    
    def get_audio_energy(self, audio_bytes: bytes) -> float:
        """Calculate RMS energy of audio chunk."""
        if len(audio_bytes) < 2:
            return 0.0
        
        # Convert bytes to int16 samples
        samples = struct.unpack(f"{len(audio_bytes)//2}h", audio_bytes)
        
        # Calculate RMS
        sum_squares = sum(s * s for s in samples)
        rms = (sum_squares / len(samples)) ** 0.5
        return rms
    
    def is_silence(self, audio_bytes: bytes) -> bool:
        """Check if audio chunk is silence."""
        energy = self.get_audio_energy(audio_bytes)
        return energy < SILENCE_THRESHOLD
    
    def update_vad(self, audio_bytes: bytes) -> bool:
        """
        Update voice activity detection state.
        Returns True if speech should stop (silence detected).
        """
        current_time = time.time()
        is_silent = self.is_silence(audio_bytes)
        
        if not is_silent:
            # Speech detected
            self.silence_start = None
            if not self.has_speech:
                self.speech_start = current_time
                self.has_speech = True
        else:
            # Silence detected
            if self.has_speech and self.silence_start is None:
                self.silence_start = current_time
        
        # Check if we should stop
        if self.has_speech and self.silence_start:
            speech_duration = self.silence_start - self.speech_start
            silence_duration = current_time - self.silence_start
            
            # Stop if enough speech and enough silence
            if (speech_duration >= MIN_SPEECH_DURATION and 
                silence_duration >= SILENCE_DURATION):
                return True
        
        return False
    
    def stop(self) -> None:
        self.is_running = False
        if self.stream:
            self.stream.stop_stream()
            self.stream.close()
    
    def terminate(self) -> None:
        self.stop()
        self.p.terminate()


# ============================================================================
# AUDIO PLAYBACK
# ============================================================================

class AudioPlayer:
    """Audio playback for TTS responses."""
    
    def __init__(self):
        self.p = pyaudio.PyAudio()
        self.stream = None
        self.is_playing = False
        self.buffer = bytearray()
        self.lock = threading.Lock()
    
    def start(self) -> None:
        self.stream = self.p.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=OUTPUT_SAMPLE_RATE,
            output=True,
            frames_per_buffer=1024
        )
        self.is_playing = True
    
    def add_audio(self, audio_bytes: bytes) -> None:
        """Add audio to playback buffer."""
        with self.lock:
            self.buffer.extend(audio_bytes)
    
    def play_buffer(self) -> None:
        """Play all buffered audio."""
        if self.stream and self.is_playing:
            with self.lock:
                if self.buffer:
                    self.stream.write(bytes(self.buffer))
                    self.buffer.clear()
    
    def write_direct(self, audio_bytes: bytes) -> None:
        """Write audio directly to stream."""
        if self.stream and self.is_playing:
            self.stream.write(audio_bytes)
    
    def stop(self) -> None:
        self.is_playing = False
        if self.stream:
            self.stream.stop_stream()
            self.stream.close()
    
    def terminate(self) -> None:
        self.stop()
        self.p.terminate()


# ============================================================================
# SIMPLE INPUT HANDLER
# ============================================================================

class SimpleInputHandler:
    """Simple input handler for quit command."""
    
    def __init__(self):
        self.input_queue = asyncio.Queue()
        self.running = True
        self.thread = None
        self._loop = None
    
    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Start input listener thread."""
        self._loop = loop
        self.thread = threading.Thread(target=self._listen, daemon=True)
        self.thread.start()
    
    def _listen(self) -> None:
        """Listen for input in background thread."""
        while self.running:
            try:
                line = input()
                if self._loop and self._loop.is_running():
                    self._loop.call_soon_threadsafe(
                        self.input_queue.put_nowait, line.strip()
                    )
            except (EOFError, KeyboardInterrupt):
                break
            except Exception:
                break
    
    def stop(self) -> None:
        self.running = False
    
    async def check_quit(self) -> bool:
        """Check if user wants to quit."""
        try:
            line = self.input_queue.get_nowait()
            return line.lower() in ['q', 'quit', 'exit']
        except asyncio.QueueEmpty:
            return False


# ============================================================================
# DEMO CLIENT
# ============================================================================

class DemoClient:
    """Demo client for testing Jarvis server."""
    
    def __init__(self, server_url: str, device_index: int, auth_token: str = DEFAULT_AUTH_TOKEN):
        self.server_url = server_url
        self.device_index = device_index
        self.auth_token = auth_token
        
        # Build URL with token
        if '?' in self.server_url:
            self.full_url = f"{self.server_url}&token={self.auth_token}"
        else:
            self.full_url = f"{self.server_url}?token={self.auth_token}"
        
        self.ws = None
        self.is_connected = False
        self.is_recording = False
        self.is_processing = False
        
        self.audio_capture = AudioCapture(device_index)
        self.audio_player = AudioPlayer()
        self.input_handler = SimpleInputHandler()
        
        # State
        self.current_transcript = ""
        self.current_response = ""
        self.session_active = False
        
        # Statistics
        self.stats = {
            'session_start': None,
            'first_audio_sent': None,
            'speech_end': None,
            'first_response_audio': None,
            'session_end': None,
        }
    
    async def connect(self) -> bool:
        """Connect to server."""
        try:
            self.ws = await websockets.connect(
                self.full_url,
                ping_interval=30,
                ping_timeout=10
            )
            self.is_connected = True
            print(f"✓ Connected to {self.server_url}")
            return True
        except websockets.exceptions.InvalidStatusCode as e:
            if e.status_code == 401:
                print(f"✗ Authentication failed! Check your token.")
            else:
                print(f"✗ Connection failed: HTTP {e.status_code}")
            return False
        except Exception as e:
            print(f"✗ Failed to connect: {e}")
            return False
    
    async def disconnect(self) -> None:
        """Disconnect from server."""
        if self.ws:
            await self.ws.close()
        self.is_connected = False
    
    async def start_session(self) -> None:
        """Start a new voice session."""
        if not self.is_connected:
            return
        
        self.session_active = True
        self.current_transcript = ""
        self.current_response = ""
        self.stats['session_start'] = time.time()
        self.stats['first_audio_sent'] = None
        self.stats['speech_end'] = None
        self.stats['first_response_audio'] = None
        
        # Send session start message
        msg = create_message(MessageType.SESSION_START, client_type="demo")
        await self.ws.send(json.dumps(msg))
    
    async def send_audio(self, audio_bytes: bytes) -> None:
        """Send audio chunk to server."""
        if not self.is_connected or not self.session_active:
            return
        
        if not self.stats['first_audio_sent']:
            self.stats['first_audio_sent'] = time.time()
        
        # Send as raw binary for efficiency
        await self.ws.send(audio_bytes)
    
    async def end_speech(self) -> None:
        """Signal end of speech to server."""
        if not self.is_connected or not self.session_active:
            return
        
        self.stats['speech_end'] = time.time()
        msg = create_message(MessageType.END_OF_SPEECH)
        await self.ws.send(json.dumps(msg))
    
    async def handle_server_message(self, message: str) -> None:
        """Handle message from server."""
        try:
            data = parse_message(message)
            msg_type = data.get("type", "")
            
            if msg_type == MessageType.SESSION_STARTED.value:
                session_id = data.get("session_id", "")
                print(f"  Session ID: {session_id}")
            
            elif msg_type == MessageType.SESSION_READY.value:
                print("\n🎤 SPEAK NOW... (press ENTER to stop)\n")
            
            elif msg_type == MessageType.TRANSCRIPT.value:
                text = data.get("text", "")
                is_final = data.get("is_final", False)
                self.current_transcript = text
                # Update display
                status = "✓" if is_final else "..."
                sys.stdout.write(f"\r\033[K📝 {text} {status}")
                sys.stdout.flush()
            
            elif msg_type == MessageType.PROCESSING.value:
                stage = data.get("stage", "")
                message = data.get("message", "")
                print(f"\n\n⏳ {message}")
            
            elif msg_type == MessageType.RESPONSE_TEXT.value:
                text = data.get("text", "")
                chunk_idx = data.get("chunk_index", 0)
                self.current_response += text + " "
                print(f"  [{chunk_idx}] {text}")
            
            elif msg_type == MessageType.AUDIO_RESPONSE.value:
                audio_b64 = data.get("data", "")
                if audio_b64:
                    audio_bytes = base64.b64decode(audio_b64)
                    
                    if not self.stats['first_response_audio']:
                        self.stats['first_response_audio'] = time.time()
                    
                    # Play audio directly
                    self.audio_player.write_direct(audio_bytes)
            
            elif msg_type == MessageType.DONE.value:
                self.session_active = False
                self.stats['session_end'] = time.time()
                self.is_processing = False
                print("\n\n✅ Response complete")
                self.display_stats()
            
            elif msg_type == MessageType.ERROR.value:
                error_msg = data.get("message", "Unknown error")
                print(f"\n✗ Error: {error_msg}")
                self.session_active = False
                self.is_processing = False
            
            elif msg_type == MessageType.PONG.value:
                pass  # Heartbeat response
            
            elif msg_type == MessageType.STATS.value:
                # Detailed statistics from server
                self.display_server_stats(data)
                
        except json.JSONDecodeError:
            pass
    
    def display_server_stats(self, data: dict) -> None:
        """Display detailed statistics from server."""
        latencies = data.get("latencies", {})
        metrics = data.get("metrics", {})
        
        print("\n" + "=" * 50)
        print("📊 DETAILED SERVER STATISTICS")
        print("=" * 50)
        
        # Key latencies
        if latencies.get("voice_response_latency_ms"):
            vrl = latencies["voice_response_latency_ms"]
            print(f"\n⭐ Voice Response Latency: {vrl}ms")
            if vrl < 1500:
                print("   Rating: ⭐⭐⭐ Excellent")
            elif vrl < 2500:
                print("   Rating: ⭐⭐ Good")
            else:
                print("   Rating: ⭐ Needs improvement")
        
        if latencies.get("time_to_first_llm_token_ms"):
            print(f"\n🤖 Time to First LLM Token: {latencies['time_to_first_llm_token_ms']}ms")
        
        if latencies.get("tts_latency_ms"):
            print(f"🔊 TTS Latency: {latencies['tts_latency_ms']}ms")
        
        if latencies.get("stt_processing_ms"):
            print(f"📝 STT Processing: {latencies['stt_processing_ms']}ms")
        
        # Network stats
        print(f"\n🌐 Audio sent: {metrics.get('audio_bytes_sent', 0)/1024:.1f} KB")
        print(f"🌐 Audio received: {metrics.get('audio_bytes_received', 0)/1024:.1f} KB")
        
        print("=" * 50)
    
    def display_stats(self) -> None:
        """Display session statistics."""
        print("\n" + "-" * 40)
        print("📊 Session Stats:")
        
        if self.stats['first_audio_sent'] and self.stats['session_start']:
            startup = self.stats['first_audio_sent'] - self.stats['session_start']
            print(f"   Startup time: {startup:.3f}s")
        
        if self.stats['speech_end'] and self.stats['first_response_audio']:
            latency = self.stats['first_response_audio'] - self.stats['speech_end']
            print(f"   Response latency: {latency:.3f}s ⭐")
        
        if self.stats['session_end'] and self.stats['session_start']:
            total = self.stats['session_end'] - self.stats['session_start']
            print(f"   Total session: {total:.2f}s")
        
        print("-" * 40)
    
    async def recording_loop(self) -> None:
        """Main recording loop - captures and sends audio with silence detection."""
        self.audio_capture.start()
        self.is_recording = True
        
        loop = asyncio.get_event_loop()
        
        print("🎤 Listening... (speak now, will auto-stop on silence)")
        
        while self.is_recording and self.session_active:
            try:
                # Capture audio chunk
                chunk = await loop.run_in_executor(None, self.audio_capture.read_chunk)
                if chunk:
                    # Send audio to server
                    await self.send_audio(chunk)
                    
                    # Check for silence
                    should_stop = self.audio_capture.update_vad(chunk)
                    if should_stop:
                        print("\n✓ Silence detected, stopping...")
                        break
                
                await asyncio.sleep(0.001)  # Small yield
                
            except Exception as e:
                print(f"\n✗ Recording error: {e}")
                break
        
        self.is_recording = False
        self.audio_capture.stop()
        
        # Signal end of speech
        if self.session_active:
            print("🔇 Processing...")
            self.is_processing = True
            await self.end_speech()
    
    async def receive_loop(self) -> None:
        """Receive messages from server."""
        try:
            async for message in self.ws:
                if isinstance(message, bytes):
                    # Binary audio response
                    if not self.stats['first_response_audio']:
                        self.stats['first_response_audio'] = time.time()
                    self.audio_player.write_direct(message)
                else:
                    await self.handle_server_message(message)
                
                if not self.session_active and not self.is_processing:
                    break
                    
        except websockets.exceptions.ConnectionClosed:
            print("\n⚠ Connection closed")
            self.is_connected = False
    
    async def voice_session(self) -> None:
        """Run a complete voice session."""
        # Start audio player
        self.audio_player.start()
        
        # Start session
        await self.start_session()
        
        # Run recording and receiving in parallel
        recording_task = asyncio.create_task(self.recording_loop())
        receive_task = asyncio.create_task(self.receive_loop())
        
        # Wait for recording to finish
        await recording_task
        
        # Wait for response to complete
        while self.session_active or self.is_processing:
            await asyncio.sleep(0.1)
        
        # Give a moment for final audio
        await asyncio.sleep(0.5)
        
        # Stop audio player
        self.audio_player.stop()
        
        # Cancel receive task if still running
        receive_task.cancel()
        try:
            await receive_task
        except asyncio.CancelledError:
            pass
    
    async def run(self) -> None:
        """Main client loop with automatic sessions."""
        print("\n" + "=" * 60)
        print("  JARVIS DEMO CLIENT - AUTO MODE")
        print("=" * 60)
        print(f"\n  Server: {self.server_url}")
        print(f"  Token: {self.auth_token[:4]}...{self.auth_token[-4:]}")
        print(f"\n  Voice Detection:")
        print(f"    - Silence threshold: {SILENCE_THRESHOLD}")
        print(f"    - Auto-stop after {SILENCE_DURATION}s of silence")
        print(f"\n  Controls:")
        print(f"    - Type 'q' or 'quit' + ENTER to exit")
        print("\n" + "=" * 60)
        
        # Connect to server
        if not await self.connect():
            return
        
        # Start input handler
        loop = asyncio.get_running_loop()
        self.input_handler.start(loop)
        
        print("\n⏳ Starting voice session in 2 seconds...")
        print("   (Type 'q' + ENTER anytime to quit)\n")
        
        await asyncio.sleep(2)
        
        try:
            session_count = 0
            while True:
                # Check for quit command
                if await self.input_handler.check_quit():
                    print("\n👋 Goodbye!")
                    break
                
                # Reconnect if needed
                if not self.is_connected:
                    print("\n⚠ Reconnecting...")
                    if not await self.connect():
                        print("✗ Failed to reconnect. Waiting 5s...")
                        await asyncio.sleep(5)
                        continue
                
                # Start voice session
                session_count += 1
                print("\n" + "=" * 60)
                print(f"🎙️  SESSION #{session_count}")
                print("=" * 60)
                
                # Reinitialize audio components for new session
                self.audio_capture = AudioCapture(self.device_index)
                self.audio_player = AudioPlayer()
                
                await self.voice_session()
                
                print("\n⏳ Starting next session in 3 seconds...")
                print("   (Type 'q' + ENTER to quit)\n")
                
                # Wait before next session
                for i in range(30):  # 3 seconds in 0.1s increments
                    if await self.input_handler.check_quit():
                        print("\n👋 Goodbye!")
                        return
                    await asyncio.sleep(0.1)
        
        except KeyboardInterrupt:
            print("\n\n👋 Interrupted - Goodbye!")
        finally:
            self.input_handler.stop()
            await self.disconnect()
            self.audio_capture.terminate()
            self.audio_player.terminate()


# ============================================================================
# MAIN
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="Jarvis Demo Client")
    parser.add_argument(
        "--server", 
        default=DEFAULT_SERVER_URL,
        help=f"Server WebSocket URL (default: {DEFAULT_SERVER_URL})"
    )
    parser.add_argument(
        "--token",
        default=DEFAULT_AUTH_TOKEN,
        help=f"Authentication token (default: {DEFAULT_AUTH_TOKEN})"
    )
    parser.add_argument(
        "--device",
        type=int,
        default=None,
        help="Audio input device index (will prompt if not specified)"
    )
    
    args = parser.parse_args()
    
    # Select microphone
    if args.device is not None:
        device_index = args.device
        print(f"✓ Using specified device: {device_index}")
    else:
        device_index = select_microphone()
    
    # Create and run client
    client = DemoClient(args.server, device_index, args.token)
    
    try:
        asyncio.run(client.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
