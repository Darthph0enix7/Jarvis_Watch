#!/usr/bin/env python3
"""Watch Client - Full Duplex Streaming for minimal latency."""

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


class WatchClient:
    def __init__(self):
        self.pa = None
        self.mic = None
        self.spk = None
        self.server_uri = f"ws://{config.SERVER_HOST}:{config.SERVER_PORT}"
        self.is_playing = False
        self.can_listen = True
        self.response_text = ""
        self.first_audio_time = None
        self.speech_end_time = None

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
        self.spk = self.pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=config.TTS_SAMPLE_RATE,
            output=True,
            frames_per_buffer=1024
        )
        log.info("🎤 Audio devices ready")

    def cleanup(self):
        """Cleanup audio resources."""
        for stream in [self.mic, self.spk]:
            if stream:
                try:
                    stream.stop_stream()
                    stream.close()
                except:
                    pass
        if self.pa:
            self.pa.terminate()

    async def sender_task(self, ws):
        """Stream mic audio to server immediately - no buffering."""
        loop = asyncio.get_event_loop()
        
        silence_frames = 0
        max_silence_frames = int(config.VAD_SILENCE_DURATION * config.STT_SAMPLE_RATE / config.CHUNK_SIZE)
        is_speaking = False
        speech_start_time = None

        while True:
            # Don't capture while playing response (half-duplex for usability)
            if self.is_playing:
                await asyncio.sleep(0.05)
                continue

            if not self.can_listen:
                await asyncio.sleep(0.05)
                continue

            try:
                # Read audio chunk (non-blocking via executor)
                data = await loop.run_in_executor(None, self.mic.read, config.CHUNK_SIZE, False)
                
                # Calculate energy for VAD
                audio = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
                energy = np.sqrt(np.mean(audio ** 2))

                if energy > config.VAD_SILENCE_THRESHOLD:
                    if not is_speaking:
                        is_speaking = True
                        speech_start_time = time.time()
                        self.response_text = ""
                        self.first_audio_time = None
                        print("\n⚡ Voice detected, streaming...")
                        # Signal server that audio is starting
                        await ws.send(json.dumps({"type": "audio_start"}))
                    
                    silence_frames = 0
                    # CRITICAL: Send audio immediately - no accumulation!
                    await ws.send(data)

                elif is_speaking:
                    # Still in speech window, send audio but track silence
                    silence_frames += 1
                    await ws.send(data)

                    if silence_frames >= max_silence_frames:
                        # End of speech detected
                        self.speech_end_time = time.time()
                        speech_duration = self.speech_end_time - speech_start_time if speech_start_time else 0
                        print(f"✓ Speech ended ({speech_duration:.1f}s)")
                        
                        # Signal end of audio
                        await ws.send(json.dumps({"type": "audio_end"}))
                        is_speaking = False
                        silence_frames = 0
                        self.can_listen = False  # Wait for response to complete

            except IOError as e:
                # Buffer overflow - skip this chunk
                if e.errno == -9981:
                    continue
                log.error(f"Mic error: {e}")
            except Exception as e:
                log.error(f"Sender error: {e}")

            await asyncio.sleep(0)  # Yield to other tasks

    async def receiver_task(self, ws):
        """Receive and play audio immediately - no buffering."""
        loop = asyncio.get_event_loop()

        try:
            async for msg in ws:
                if isinstance(msg, bytes):
                    # Audio chunk - play immediately!
                    if self.first_audio_time is None:
                        self.first_audio_time = time.time()
                        latency = (self.first_audio_time - self.speech_end_time) * 1000 if self.speech_end_time else 0
                        print(f"\n🔊 Playing... (latency: {latency:.0f}ms)")
                    
                    self.is_playing = True
                    await loop.run_in_executor(None, self.spk.write, msg)

                else:
                    # JSON message
                    data = json.loads(msg)
                    msg_type = data.get("type")

                    if msg_type == "transcript":
                        print(f"📝 You: {data.get('text')}")

                    elif msg_type == "response_text":
                        self.response_text += data.get("text", "")
                        print(f"\r🤖 Jarvis: {self.response_text}", end="", flush=True)

                    elif msg_type == "audio_end":
                        self.is_playing = False
                        self.can_listen = True
                        total_latency = (time.time() - self.speech_end_time) if self.speech_end_time else 0
                        print(f"\n📊 Total response time: {total_latency:.2f}s")
                        print("\n🔴 Listening... (speak now)")

                    elif msg_type == "error":
                        print(f"\n❌ {data.get('message')}")
                        self.can_listen = True

        except websockets.exceptions.ConnectionClosed:
            log.info("Connection closed")
        except Exception as e:
            log.error(f"Receiver error: {e}")

    async def run(self):
        """Main loop - run sender and receiver in parallel."""
        self.init_audio()

        print("=" * 60)
        print("🎙️  Jarvis Watch Client (Streaming Mode)")
        print("=" * 60)
        print(f"Server: {self.server_uri}")
        print(f"VAD: threshold={config.VAD_SILENCE_THRESHOLD}, silence={config.VAD_SILENCE_DURATION}s")
        print("=" * 60)
        print("\n🔴 Listening... (speak now)")
        print("Press Ctrl+C to quit\n")

        try:
            async with websockets.connect(self.server_uri) as ws:
                log.info("✓ Connected to server")
                
                # Run sender and receiver in parallel (full duplex)
                await asyncio.gather(
                    self.sender_task(ws),
                    self.receiver_task(ws)
                )

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
