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
from urllib.parse import urlencode

import pyaudio
import websockets

# ============================================================================
# CONFIGURATION
# ============================================================================

CARTESIA_API_KEY = "sk_car_DAwyAsQnVVDUhqJm6mfBir"  # Replace with your actual API key
CARTESIA_BASE_URL = "wss://api.cartesia.ai/stt/websocket"

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
    
    def build_url(self) -> str:
        """Build WebSocket URL with query parameters."""
        query_string = urlencode(CARTESIA_PARAMS)
        return f"{CARTESIA_BASE_URL}?{query_string}"
    
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
            self.speech_started = True
            return True
        else:
            # Check if we've been silent long enough
            if self.speech_started and self.last_speech_time:
                silence_duration = current_time - self.last_speech_time
                if silence_duration >= SILENCE_DURATION and not self.vad_triggered:
                    self.vad_triggered = True
                    return False
            return False
    
    async def send_audio(self, audio_data: bytes) -> None:
        """Send audio chunk to Cartesia."""
        if self.ws and self.is_connected and not self.session_done:
            try:
                await self.ws.send(audio_data)
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
        # Append to full transcript
        if self.full_transcript and not self.full_transcript.endswith(" "):
            self.full_transcript += " "
        self.full_transcript += text
        self.current_partial = ""
        
        # Update display
        sys.stdout.write(f"\r\033[K📝 {self.full_transcript}")
        sys.stdout.flush()
    
    def display_done(self) -> None:
        """Display final complete transcription."""
        print()  # New line after partial updates
        print("\n" + "=" * 60)
        print("✅ TRANSCRIPTION COMPLETE (Silence Detected)")
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
                        # Session complete - break out to trigger cleanup
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
                
                if is_final:
                    self.display_final(text)
                else:
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
                await client.send_finalize()
                # Wait for final transcripts
                await asyncio.sleep(0.5)
                # Mark session as done
                client.session_done = True
                client.display_done()
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

async def main() -> None:
    """Main entry point for Jarvis STT."""
    print("\n" + "=" * 60)
    print("  PROJECT JARVIS - Phase 1: Speech-to-Text")
    print("  Powered by Cartesia Ink API")
    print("=" * 60)
    
    # Step 1: Microphone setup
    device_index = select_microphone()
    
    # Initialize components
    audio = AudioCapture(device_index)
    client = CartesiaSTTClient()
    audio_queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    stop_event = asyncio.Event()
    
    try:
        # Step 2: Start audio capture
        audio.start()
        
        # Step 3: Calibrate ambient noise (brief)
        await calibration_task(audio, client)
        
        # Step 4: Connect to Cartesia
        await client.connect()
        
        # Step 5: Run concurrent tasks
        await asyncio.gather(
            audio_capture_task(audio, audio_queue, stop_event),
            audio_sender_task(client, audio_queue, stop_event),
            message_receiver_task(client, stop_event)
        )
        
    except KeyboardInterrupt:
        print("\n\n⚠ Interrupted by user")
        # Still display what we got so far
        if client.full_transcript.strip():
            print("\n" + "=" * 60)
            print("PARTIAL TRANSCRIPTION")
            print("=" * 60)
            print(f"\n{client.full_transcript.strip()}\n")
            print("=" * 60)
    except Exception as e:
        print(f"\n✗ Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        # Cleanup
        stop_event.set()
        audio.stop()
        
        # Send finalize only if session not already done
        if client.is_connected and not client.session_done:
            await client.send_finalize()
            await asyncio.sleep(0.3)  # Brief wait for final transcripts
        
        # Close connection gracefully
        if client.is_connected:
            await client.send_done()
        await client.close()
        
        if not client.session_done:
            print("\n✓ Session closed")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
