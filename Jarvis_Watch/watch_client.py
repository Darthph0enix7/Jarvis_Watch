#!/usr/bin/env python3
"""Watch Client - Captures mic, sends to server, plays response."""

import asyncio
import json
import logging
import time

import pyaudio
import websockets
import numpy as np

import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

def get_energy(audio_bytes: bytes) -> float:
    """Calculate audio energy for VAD."""
    audio = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    return np.sqrt(np.mean(audio ** 2))

class WatchClient:
    def __init__(self):
        self.pa = None
        self.mic = None
        self.spk = None
        self.server_uri = f"ws://{config.SERVER_HOST}:{config.SERVER_PORT}"
    
    def init_audio(self):
        """Initialize audio devices."""
        self.pa = pyaudio.PyAudio()
        self.mic = self.pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=config.STT_SAMPLE_RATE,
            input=True,
            frames_per_buffer=config.CHUNK_SIZE
        )
        log.info("🎤 Microphone ready")
    
    def init_speaker(self, sample_rate: int):
        """Initialize speaker."""
        if self.spk:
            self.spk.close()
        self.spk = self.pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=sample_rate,
            output=True,
            frames_per_buffer=1024
        )
    
    def cleanup(self):
        """Cleanup audio."""
        for s in [self.mic, self.spk]:
            if s:
                try:
                    s.stop_stream()
                    s.close()
                except:
                    pass
        if self.pa:
            self.pa.terminate()
    
    async def record_until_silence(self) -> list[bytes]:
        """Record audio until silence detected."""
        chunks = []
        loop = asyncio.get_event_loop()
        silence_frames = 0
        speech_started = False
        frames_for_silence = int(config.VAD_SILENCE_DURATION * config.STT_SAMPLE_RATE / config.CHUNK_SIZE)
        
        print("\n🔴 Listening...")
        
        while True:
            try:
                data = await loop.run_in_executor(None, self.mic.read, config.CHUNK_SIZE, False)
                energy = get_energy(data)
                
                if energy > config.VAD_SILENCE_THRESHOLD:
                    speech_started = True
                    silence_frames = 0
                    chunks.append(data)
                else:
                    if speech_started:
                        chunks.append(data)
                        silence_frames += 1
                        if silence_frames >= frames_for_silence:
                            print("✓ Speech ended\n")
                            break
            except Exception as e:
                log.error(f"Recording error: {e}")
                break
        
        return chunks
    
    async def process_one_request(self, ws):
        """Record → Send → Receive → Play."""
        timings = {
            "record_start": time.time(),
            "record_end": None,
            "send_start": None,
            "send_end": None,
            "first_response": None,
            "transcript_received": None,
            "first_audio": None,
            "complete": None
        }
        
        # Record
        audio_chunks = await self.record_until_silence()
        timings["record_end"] = time.time()
        
        if not audio_chunks:
            print("❌ No audio captured")
            return
        
        # Send to server
        print("📤 Processing...")
        timings["send_start"] = time.time()
        
        # Send start signal
        await ws.send(json.dumps({"type": "audio_start"}))
        
        # Stream audio chunks
        for chunk in audio_chunks:
            await ws.send(chunk)
        
        # Send end signal
        await ws.send(json.dumps({"type": "audio_end"}))
        timings["send_end"] = time.time()
        
        # Receive and play response
        response_text = ""
        
        try:
            async for msg in ws:
                if isinstance(msg, bytes):
                    # Audio chunk
                    if self.spk:
                        if timings["first_audio"] is None:
                            timings["first_audio"] = time.time()
                        await asyncio.get_event_loop().run_in_executor(None, self.spk.write, msg)
                else:
                    if timings["first_response"] is None:
                        timings["first_response"] = time.time()
                    
                    data = json.loads(msg)
                    msg_type = data.get("type")
                    
                    if msg_type == "transcript":
                        timings["transcript_received"] = time.time()
                        print(f"📝 You: {data.get('text')}")
                    
                    elif msg_type == "response_text":
                        response_text += data.get("text", "")
                        print(f"\r🤖 Jarvis: {response_text}", end="", flush=True)
                    
                    elif msg_type == "audio_start":
                        print()
                        rate = data.get("sample_rate", config.TTS_SAMPLE_RATE)
                        self.init_speaker(rate)
                        print("🔊 Playing...")
                    
                    elif msg_type == "audio_end":
                        timings["complete"] = time.time()
                        
                        # Calculate metrics (excluding user speaking time)
                        send_time = timings["send_end"] - timings["send_start"]
                        server_processing = timings["first_audio"] - timings["send_end"] if timings["first_audio"] else 0
                        playback_time = timings["complete"] - timings["first_audio"] if timings["first_audio"] else 0
                        total_system_time = timings["complete"] - timings["send_end"]
                        
                        print(f"\n📊 System Latencies (excluding speaking time):")
                        print(f"   Upload: {send_time*1000:.0f}ms")
                        print(f"   Server processing (STT+LLM+TTS): {server_processing*1000:.0f}ms")
                        print(f"   Audio playback: {playback_time:.2f}s")
                        print(f"   Total system time: {total_system_time:.2f}s\n")
                        break
                    
                    elif msg_type == "error":
                        print(f"❌ {data.get('message')}")
                        break
        except Exception as e:
            log.error(f"Response error: {e}")
    
    async def run(self):
        """Main loop."""
        self.init_audio()
        
        print("=" * 60)
        print("🎙️  Jarvis Watch Client")
        print("=" * 60)
        print(f"Server: {self.server_uri}")
        print(f"VAD: {config.VAD_SILENCE_THRESHOLD} threshold | {config.VAD_SILENCE_DURATION}s silence")
        print("=" * 60)
        print("\nPress Ctrl+C to quit")
        
        try:
            async with websockets.connect(self.server_uri) as ws:
                log.info("✓ Connected to server")
                
                while True:
                    await self.process_one_request(ws)
        
        except websockets.exceptions.ConnectionClosed:
            log.error("Connection closed")
        except Exception as e:
            log.error(f"Error: {e}")
        finally:
            self.cleanup()

async def main():
    client = WatchClient()
    try:
        await client.run()
    except KeyboardInterrupt:
        print("\n👋 Goodbye!")

if __name__ == "__main__":
    asyncio.run(main())
