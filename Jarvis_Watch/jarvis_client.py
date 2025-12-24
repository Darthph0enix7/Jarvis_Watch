#!/usr/bin/env python3
"""Watch Client - Captures voice, sends to server, plays response."""

import asyncio
import json
import logging
import argparse
from collections import deque

import pyaudio
import websockets
import numpy as np
import torch

try:
    from pynput import keyboard
    PYNPUT_AVAILABLE = True
except ImportError:
    PYNPUT_AVAILABLE = False

from config import get_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

# =============================================================================
# VAD
# =============================================================================

class VAD:
    def __init__(self, threshold: float):
        self.threshold = threshold
        self.model = None
        try:
            self.model, _ = torch.hub.load('snakers4/silero-vad', 'silero_vad', trust_repo=True)
            self.model.eval()
            log.info("✓ VAD loaded")
        except Exception as e:
            log.warning(f"⚠️ VAD fallback: {e}")
    
    def reset(self):
        if self.model:
            self.model.reset_states()
    
    def is_speech(self, audio: bytes, sample_rate: int) -> tuple[bool, float]:
        audio_np = np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32768.0
        if self.model:
            conf = self.model(torch.from_numpy(audio_np), sample_rate).item()
            return conf > self.threshold, conf
        energy = np.sqrt(np.mean(audio_np ** 2))
        return energy > 0.01, min(energy * 10, 1.0)

# =============================================================================
# Keyboard
# =============================================================================

class KeyController:
    def __init__(self):
        self._active = False
        self._quit = False
        self._toggle_event = asyncio.Event()
        self._listener = None
        if PYNPUT_AVAILABLE:
            self._start()
    
    def _start(self):
        def on_press(key):
            try:
                if hasattr(key, 'char') and key.char in ['a', 'A']:
                    self._active = not self._active
                    self._toggle_event.set()
                elif hasattr(key, 'char') and key.char in ['q', 'Q']:
                    self._quit = True
                    self._toggle_event.set()
                    return False
            except:
                pass
        self._listener = keyboard.Listener(on_press=on_press)
        self._listener.start()
    
    @property
    def is_active(self) -> bool:
        return self._active
    
    @property
    def quit(self) -> bool:
        return self._quit
    
    async def wait_toggle(self):
        self._toggle_event.clear()
        await self._toggle_event.wait()
    
    def deactivate(self):
        self._active = False
    
    def stop(self):
        if self._listener:
            self._listener.stop()

# =============================================================================
# Client
# =============================================================================

class WatchClient:
    def __init__(self):
        self.cfg = get_config()
        self.keys = KeyController()
        self.vad = VAD(self.cfg.vad.threshold)
        
        self.pa = None
        self.mic = None
        self.spk = None
        self.ws = None
        
        self.server_uri = f"ws://{self.cfg.server.host}:{self.cfg.server.port}"
        self._running = False
    
    async def run(self):
        self._running = True
        self._init_audio()
        
        log.info(f"🎙️ Watch Client | Server: {self.server_uri}")
        log.info("Press 'A' to talk | 'Q' to quit")
        
        try:
            await self._connect_and_loop()
        finally:
            self._cleanup_audio()
            self.keys.stop()
    
    def _init_audio(self):
        self.pa = pyaudio.PyAudio()
        self.mic = self.pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=self.cfg.stt.sample_rate,
            input=True,
            frames_per_buffer=self.cfg.client.chunk_size
        )
    
    def _init_speaker(self, sample_rate: int):
        if self.spk:
            self.spk.close()
        self.spk = self.pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=sample_rate,
            output=True,
            frames_per_buffer=1024
        )
    
    def _cleanup_audio(self):
        for s in [self.mic, self.spk]:
            if s:
                try:
                    s.stop_stream()
                    s.close()
                except:
                    pass
        if self.pa:
            self.pa.terminate()
    
    async def _connect_and_loop(self):
        attempts = 0
        while self._running and not self.keys.quit:
            try:
                async with websockets.connect(self.server_uri) as ws:
                    self.ws = ws
                    attempts = 0
                    log.info("✓ Connected to server")
                    await self._main_loop()
            except Exception as e:
                log.error(f"Connection error: {e}")
                attempts += 1
                if attempts >= self.cfg.client.max_reconnects:
                    break
                await asyncio.sleep(self.cfg.client.reconnect_delay)
    
    async def _main_loop(self):
        while self._running and not self.keys.quit:
            print("\n⏸️  Press 'A' to speak...")
            await self.keys.wait_toggle()
            
            if self.keys.quit:
                break
            if not self.keys.is_active:
                continue
            
            # Record
            print("🔴 Recording... (press 'A' to stop)")
            audio_chunks = await self._record_until_stop()
            
            if not audio_chunks:
                print("❌ No audio captured")
                continue
            
            # Send to server
            print("📤 Processing...")
            await self._send_voice_request(audio_chunks)
    
    async def _record_until_stop(self) -> list[bytes]:
        chunks = []
        loop = asyncio.get_event_loop()
        self.vad.reset()
        
        speech_frames = 0
        silence_frames = 0
        min_speech = int(self.cfg.vad.min_speech_ms / 1000 * self.cfg.stt.sample_rate / self.cfg.client.chunk_size)
        min_silence = int(self.cfg.vad.min_silence_ms / 1000 * self.cfg.stt.sample_rate / self.cfg.client.chunk_size)
        
        while self.keys.is_active and not self.keys.quit:
            try:
                data = await loop.run_in_executor(None, self.mic.read, self.cfg.client.chunk_size, False)
                chunks.append(data)
                
                is_speech, _ = self.vad.is_speech(data, self.cfg.stt.sample_rate)
                if is_speech:
                    speech_frames += 1
                    silence_frames = 0
                else:
                    silence_frames += 1
                    if speech_frames >= min_speech and silence_frames >= min_silence:
                        self.keys.deactivate()
                        break
            except:
                break
        
        return chunks
    
    async def _send_voice_request(self, audio_chunks: list[bytes]):
        if not self.ws:
            return
        
        # Send voice request
        request = {
            "type": "voice_request",
            "audio": [list(chunk) for chunk in audio_chunks]
        }
        await self.ws.send(json.dumps(request))
        
        # Receive response
        response_text = ""
        try:
            async for msg in self.ws:
                if isinstance(msg, bytes):
                    # Audio chunk
                    if self.spk:
                        self.spk.write(msg)
                else:
                    data = json.loads(msg)
                    msg_type = data.get("type")
                    
                    if msg_type == "transcript":
                        print(f"📝 You said: {data.get('text')}")
                    
                    elif msg_type == "response_text":
                        response_text += data.get("text", "")
                        print(f"\r🤖 {response_text}", end="", flush=True)
                    
                    elif msg_type == "audio_start":
                        print()
                        rate = data.get("sample_rate", 24000)
                        self._init_speaker(rate)
                        print("🔊 Playing response...")
                    
                    elif msg_type == "audio_end":
                        print("✓ Done")
                        break
                    
                    elif msg_type == "error":
                        print(f"❌ {data.get('message')}")
                        break
        except Exception as e:
            log.error(f"Response error: {e}")


async def main():
    parser = argparse.ArgumentParser(description="Jarvis Watch Client")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    client = WatchClient()
    try:
        await client.run()
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    asyncio.run(main())
