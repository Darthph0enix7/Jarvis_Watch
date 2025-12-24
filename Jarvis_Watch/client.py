#!/usr/bin/env python3
"""Watch Client Simulator - Streams voice to server, plays responses."""

import asyncio
import json
import time
import argparse
import logging
from collections import deque
from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional

import pyaudio
import websockets
import torch
import numpy as np

try:
    from pynput import keyboard
    PYNPUT_AVAILABLE = True
except ImportError:
    PYNPUT_AVAILABLE = False

# =============================================================================
# Config
# =============================================================================

@dataclass
class Config:
    SAMPLE_RATE: int = 16000
    CHANNELS: int = 1
    CHUNK_SIZE: int = 512
    FORMAT: int = pyaudio.paInt16
    VAD_THRESHOLD: float = 0.5
    MIN_SPEECH_MS: int = 250
    MIN_SILENCE_MS: int = 500
    SERVER_URI: str = "ws://localhost:8765"
    RECONNECT_DELAY: float = 2.0
    MAX_RECONNECTS: int = 10

CFG = Config()

# =============================================================================
# State
# =============================================================================

class State(Enum):
    DISCONNECTED = auto()
    CONNECTING = auto()
    IDLE = auto()
    LISTENING = auto()
    PROCESSING = auto()
    PLAYING = auto()

class SpeechState(Enum):
    SILENCE = auto()
    SPEECH_START = auto()
    SPEAKING = auto()

# =============================================================================
# VAD
# =============================================================================

class VAD:
    def __init__(self):
        self.model = None
        try:
            self.model, _ = torch.hub.load('snakers4/silero-vad', 'silero_vad', trust_repo=True)
            self.model.eval()
            logging.info("✓ Silero VAD loaded")
        except Exception as e:
            logging.warning(f"⚠️ VAD fallback to energy: {e}")
    
    def reset(self):
        if self.model:
            self.model.reset_states()
    
    def is_speech(self, audio: bytes) -> tuple[bool, float]:
        audio_np = np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32768.0
        if self.model:
            conf = self.model(torch.from_numpy(audio_np), CFG.SAMPLE_RATE).item()
            return conf >= CFG.VAD_THRESHOLD, conf
        energy = np.sqrt(np.mean(audio_np ** 2))
        return energy > 0.01, min(energy * 10, 1.0)

# =============================================================================
# Audio Playback Buffer (Fixed for smooth playback)
# =============================================================================

class PlaybackBuffer:
    def __init__(self):
        self.chunks = deque()
        self.lock = asyncio.Lock()
    
    async def write(self, chunk: bytes):
        async with self.lock:
            self.chunks.append(chunk)
    
    async def read(self) -> Optional[bytes]:
        async with self.lock:
            return self.chunks.popleft() if self.chunks else None
    
    async def clear(self):
        async with self.lock:
            self.chunks.clear()
    
    @property
    def size(self) -> int:
        return len(self.chunks)

# =============================================================================
# Activation Controller
# =============================================================================

class Activation:
    def __init__(self):
        self._active = False
        self._quit = False
        self._listener = None
        if PYNPUT_AVAILABLE:
            self._start_listener()
    
    def _start_listener(self):
        def on_press(key):
            try:
                if hasattr(key, 'char') and key.char in ['a', 'A']:
                    self._active = not self._active
                    logging.info(f"{'🟢 ACTIVATED' if self._active else '🔴 DEACTIVATED'}")
                elif hasattr(key, 'char') and key.char in ['q', 'Q']:
                    self._quit = True
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
    
    def deactivate(self):
        if self._active:
            self._active = False
            logging.info("🔴 Auto-deactivated")
    
    def stop(self):
        if self._listener:
            self._listener.stop()

# =============================================================================
# Watch Client
# =============================================================================

class WatchClient:
    def __init__(self, server_uri: str = CFG.SERVER_URI):
        self.server_uri = server_uri
        self.state = State.DISCONNECTED
        self.speech_state = SpeechState.SILENCE
        self._running = False
        
        self.vad = VAD()
        self.buffer = PlaybackBuffer()
        self.activation = Activation()
        
        self.pa: Optional[pyaudio.PyAudio] = None
        self.mic: Optional[pyaudio.Stream] = None
        self.spk: Optional[pyaudio.Stream] = None
        self.ws = None
        
        self._speech_start = None
        self._silence_start = None
        self.stats = {"sent": 0, "recv": 0}
    
    async def start(self):
        self._running = True
        self._init_audio()
        logging.info(f"🎙️ Client started | Server: {self.server_uri}")
        logging.info("Press 'A' to activate | 'Q' to quit")
        await self._main_loop()
    
    async def stop(self):
        self._running = False
        if self.ws:
            await self.ws.close()
        self._cleanup_audio()
        self.activation.stop()
        logging.info(f"📊 Sent: {self.stats['sent']} | Recv: {self.stats['recv']}")
    
    def _init_audio(self):
        self.pa = pyaudio.PyAudio()
        self.mic = self.pa.open(
            format=CFG.FORMAT, channels=CFG.CHANNELS, rate=CFG.SAMPLE_RATE,
            input=True, frames_per_buffer=CFG.CHUNK_SIZE
        )
        self.spk = self.pa.open(
            format=CFG.FORMAT, channels=CFG.CHANNELS, rate=CFG.SAMPLE_RATE,
            output=True, frames_per_buffer=CFG.CHUNK_SIZE
        )
    
    def _cleanup_audio(self):
        for stream in [self.mic, self.spk]:
            if stream:
                stream.stop_stream()
                stream.close()
        if self.pa:
            self.pa.terminate()
    
    async def _main_loop(self):
        attempts = 0
        while self._running and not self.activation.quit:
            try:
                self.state = State.CONNECTING
                logging.info(f"🔌 Connecting...")
                async with websockets.connect(self.server_uri, ping_interval=20) as ws:
                    self.ws = ws
                    self.state = State.IDLE
                    attempts = 0
                    logging.info("✓ Connected")
                    await asyncio.gather(
                        self._ear_task(),
                        self._mouth_task(),
                        self._playback_task()
                    )
            except Exception as e:
                logging.warning(f"⚠️ {e}")
            
            self.state = State.DISCONNECTED
            self.ws = None
            attempts += 1
            if attempts >= CFG.MAX_RECONNECTS:
                break
            if self._running and not self.activation.quit:
                await asyncio.sleep(CFG.RECONNECT_DELAY)
    
    async def _ear_task(self):
        """Capture mic → VAD → send to server"""
        loop = asyncio.get_event_loop()
        while self._running and self.ws and not self.activation.quit:
            try:
                chunk = await loop.run_in_executor(
                    None, lambda: self.mic.read(CFG.CHUNK_SIZE, exception_on_overflow=False)
                )
                is_speech, conf = self.vad.is_speech(chunk)
                should_send = self.activation.is_active and is_speech
                await self._update_speech(should_send, conf)
                
                if self.speech_state in (SpeechState.SPEECH_START, SpeechState.SPEAKING):
                    await self.ws.send(chunk)
                    self.stats["sent"] += 1
                
                await asyncio.sleep(0.001)
            except Exception as e:
                logging.error(f"Ear: {e}")
                break
    
    async def _update_speech(self, is_speech: bool, conf: float):
        now = time.time()
        if is_speech:
            self._silence_start = None
            if self.speech_state == SpeechState.SILENCE:
                self._speech_start = now
                self.speech_state = SpeechState.SPEECH_START
                self.state = State.LISTENING
                await self.ws.send(json.dumps({"type": "stream_start", "rate": CFG.SAMPLE_RATE}))
                await self.buffer.clear()
                logging.info(f"🎤 Speaking ({conf:.2f})")
            elif self.speech_state == SpeechState.SPEECH_START and self._speech_start:
                if (now - self._speech_start) * 1000 >= CFG.MIN_SPEECH_MS:
                    self.speech_state = SpeechState.SPEAKING
        else:
            if self.speech_state in (SpeechState.SPEECH_START, SpeechState.SPEAKING):
                if self._silence_start is None:
                    self._silence_start = now
                if (now - self._silence_start) * 1000 >= CFG.MIN_SILENCE_MS:
                    self.speech_state = SpeechState.SILENCE
                    self.state = State.PROCESSING
                    self._speech_start = None
                    await self.ws.send(json.dumps({"type": "stream_end"}))
                    self.vad.reset()
                    self.activation.deactivate()
                    logging.info("🔇 Done")
    
    async def _mouth_task(self):
        """Receive from server → buffer or display"""
        while self._running and self.ws and not self.activation.quit:
            try:
                msg = await self.ws.recv()
                if isinstance(msg, bytes):
                    await self.buffer.write(msg)
                    self.stats["recv"] += 1
                    if self.state == State.PROCESSING:
                        self.state = State.PLAYING
                else:
                    data = json.loads(msg)
                    t = data.get("type")
                    if t == "transcription":
                        logging.info(f"📝 {data.get('text', '')}")
                    elif t == "response":
                        logging.info(f"🤖 {data.get('text', '')}")
            except websockets.exceptions.ConnectionClosed:
                break
            except Exception as e:
                logging.error(f"Mouth: {e}")
                break
    
    async def _playback_task(self):
        """Play buffered audio smoothly"""
        loop = asyncio.get_event_loop()
        while self._running and not self.activation.quit:
            try:
                chunk = await self.buffer.read()
                if chunk:
                    await loop.run_in_executor(None, lambda c=chunk: self.spk.write(c))
                else:
                    if self.state == State.PLAYING and self.buffer.size == 0:
                        self.state = State.IDLE
                    await asyncio.sleep(0.005)
            except Exception as e:
                logging.error(f"Playback: {e}")
                await asyncio.sleep(0.1)

# =============================================================================
# Main
# =============================================================================

async def main():
    parser = argparse.ArgumentParser(description="Watch Client")
    parser.add_argument("--server", "-s", default=CFG.SERVER_URI)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(message)s", datefmt="%H:%M:%S"
    )
    
    client = WatchClient(server_uri=args.server)
    try:
        await client.start()
    except KeyboardInterrupt:
        pass
    finally:
        await client.stop()

if __name__ == "__main__":
    asyncio.run(main())
