#!/usr/bin/env python3
"""
Voice Assistant Demo - STT → TTS Pipeline
Press 'A' to start recording, 'A' again to stop and speak the transcription.
Press 'Q' to quit.
"""

import asyncio
import websockets
import json
import pyaudio
import urllib.parse
import uuid
import base64

try:
    from pynput import keyboard
    PYNPUT_AVAILABLE = True
except ImportError:
    PYNPUT_AVAILABLE = False
    print("Install pynput: pip install pynput")

# =============================================================================
# Config
# =============================================================================

API_KEY = "sk_car_eCzTSfNDxYxhgteweyBYr3"
VOICE_ID = "ffe42012-140d-40ab-8cc3-d3f0e957dbc9"
MODEL_TTS = "sonic-3"
MODEL_STT = "ink-whisper"

STT_SAMPLE_RATE = 16000
TTS_SAMPLE_RATE = 24000

# =============================================================================
# Keyboard Controller
# =============================================================================

class KeyController:
    def __init__(self):
        self._activated = False
        self._toggle_event = asyncio.Event()
        self._quit = False
        self._listener = None
        
        if PYNPUT_AVAILABLE:
            self._start()
    
    def _start(self):
        def on_press(key):
            try:
                if hasattr(key, 'char') and key.char in ['a', 'A']:
                    self._activated = not self._activated
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
    def is_recording(self) -> bool:
        return self._activated
    
    @property
    def quit(self) -> bool:
        return self._quit
    
    async def wait_toggle(self):
        self._toggle_event.clear()
        await self._toggle_event.wait()
    
    def stop(self):
        if self._listener:
            self._listener.stop()

# =============================================================================
# STT Provider
# =============================================================================

class CartesiaSTT:
    def __init__(self, api_key: str):
        params = {
            "api_key": api_key,
            "cartesia_version": "2024-06-10",
            "model": MODEL_STT,
            "language": "en",
            "encoding": "pcm_s16le",
            "sample_rate": str(STT_SAMPLE_RATE),
            "min_volume": "0.1",
            "max_silence_duration_secs": "0.7"
        }
        self.url = f"wss://api.cartesia.ai/stt/websocket?{urllib.parse.urlencode(params)}"
    
    async def transcribe(self, audio_gen, stop_event: asyncio.Event):
        """Transcribe audio until stop_event is set. Returns full transcript."""
        full_text = ""
        
        async with websockets.connect(self.url) as ws:
            async def sender():
                try:
                    async for chunk in audio_gen:
                        if stop_event.is_set():
                            break
                        await ws.send(chunk)
                except:
                    pass
                finally:
                    try:
                        await ws.send(json.dumps({"type": "finalize"}))
                    except:
                        pass
            
            async def receiver():
                nonlocal full_text
                try:
                    async for msg in ws:
                        if stop_event.is_set():
                            break
                        data = json.loads(msg)
                        if data.get("type") == "transcript":
                            text = data.get("text", "").strip()
                            is_final = data.get("is_final", False)
                            if text and is_final:
                                if full_text:
                                    full_text += " "
                                full_text += text
                            yield (text, is_final)
                except:
                    pass
            
            sender_task = asyncio.create_task(sender())
            try:
                async for text, is_final in receiver():
                    yield (text, is_final, full_text)
            finally:
                sender_task.cancel()
        
        return full_text

# =============================================================================
# TTS Provider  
# =============================================================================

class CartesiaTTS:
    def __init__(self, api_key: str, voice_id: str):
        params = {
            "api_key": api_key,
            "cartesia_version": "2024-06-10"
        }
        self.url = f"wss://api.cartesia.ai/tts/websocket?{urllib.parse.urlencode(params)}"
        self.voice_id = voice_id
    
    async def stream(self, text: str):
        """Stream TTS audio. Yields raw PCM bytes."""
        if not text.strip():
            return
        
        async with websockets.connect(self.url) as ws:
            context_id = str(uuid.uuid4())
            chunks = self._split(text)
            
            async def sender():
                for i, chunk in enumerate(chunks):
                    is_last = (i == len(chunks) - 1)
                    req = {
                        "model_id": MODEL_TTS,
                        "transcript": chunk,
                        "voice": {"mode": "id", "id": self.voice_id},
                        "output_format": {
                            "container": "raw",
                            "encoding": "pcm_s16le", 
                            "sample_rate": TTS_SAMPLE_RATE
                        },
                        "context_id": context_id,
                        "continue": not is_last
                    }
                    await ws.send(json.dumps(req))
                    if not is_last:
                        await asyncio.sleep(0.03)
            
            async def receiver():
                async for msg in ws:
                    data = json.loads(msg)
                    if data.get("type") == "chunk":
                        audio = data.get("data", "")
                        if audio:
                            yield base64.b64decode(audio)
                    elif data.get("type") == "done":
                        break
                    elif data.get("type") == "error":
                        print(f"\n[TTS Error] {data}")
                        break
            
            sender_task = asyncio.create_task(sender())
            try:
                async for chunk in receiver():
                    yield chunk
            finally:
                sender_task.cancel()
    
    def _split(self, text: str, max_len: int = 80) -> list[str]:
        if len(text) <= max_len:
            return [text]
        parts = text.replace("!", "!|").replace("?", "?|").replace(".", ".|").split("|")
        chunks, cur = [], ""
        for p in parts:
            p = p.strip()
            if not p:
                continue
            if len(cur) + len(p) <= max_len:
                cur = f"{cur} {p}".strip()
            else:
                if cur:
                    chunks.append(cur)
                cur = p
        if cur:
            chunks.append(cur)
        return chunks or [text]

# =============================================================================
# Audio I/O
# =============================================================================

class MicStream:
    def __init__(self):
        self.pa = pyaudio.PyAudio()
        self.stream = self.pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=STT_SAMPLE_RATE,
            input=True,
            frames_per_buffer=512
        )
    
    async def read(self):
        loop = asyncio.get_event_loop()
        while True:
            data = await loop.run_in_executor(None, self.stream.read, 512, False)
            yield data
    
    def close(self):
        self.stream.stop_stream()
        self.stream.close()
        self.pa.terminate()

class Speaker:
    def __init__(self):
        self.pa = pyaudio.PyAudio()
        self.stream = self.pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=TTS_SAMPLE_RATE,
            output=True,
            frames_per_buffer=1024
        )
    
    async def play(self, chunk: bytes):
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.stream.write, chunk)
    
    def close(self):
        self.stream.stop_stream()
        self.stream.close()
        self.pa.terminate()

# =============================================================================
# Main
# =============================================================================

async def main():
    print("=" * 60)
    print("🎙️  Voice Assistant Demo (STT → TTS)")
    print("=" * 60)
    print("Press 'A' to START recording")
    print("Press 'A' again to STOP and hear the response")
    print("Press 'Q' to quit")
    print("=" * 60)
    
    keys = KeyController()
    stt = CartesiaSTT(API_KEY)
    tts = CartesiaTTS(API_KEY, VOICE_ID)
    
    while not keys.quit:
        # Wait for activation
        print("\n⏸️  Waiting... (press 'A' to record)")
        await keys.wait_toggle()
        
        if keys.quit:
            break
        
        if not keys.is_recording:
            continue
        
        # START RECORDING
        print("\n🔴 RECORDING... (press 'A' to stop)")
        mic = MicStream()
        stop_event = asyncio.Event()
        
        full_transcript = ""
        partial = ""
        
        # Create audio generator that respects stop
        async def mic_gen():
            async for chunk in mic.read():
                if stop_event.is_set():
                    break
                yield chunk
        
        # Run STT with display
        stt_task = None
        try:
            async for text, is_final, accumulated in stt.transcribe(mic_gen(), stop_event):
                # Check for stop
                if not keys.is_recording:
                    stop_event.set()
                    break
                
                if is_final:
                    full_transcript = accumulated
                    print(f"\r📝 {full_transcript}", end="", flush=True)
                else:
                    display = f"{full_transcript} \033[90m{text}\033[0m" if full_transcript else f"\033[90m{text}\033[0m"
                    print(f"\r{display}    ", end="", flush=True)
        except Exception as e:
            print(f"\n[STT Error] {e}")
        finally:
            mic.close()
        
        print()  # Newline
        
        if not full_transcript.strip():
            print("❌ No speech detected")
            continue
        
        # PLAY TTS
        print(f"\n🔊 Speaking: \"{full_transcript[:50]}{'...' if len(full_transcript) > 50 else ''}\"")
        
        speaker = Speaker()
        try:
            chunk_count = 0
            async for audio in tts.stream(full_transcript):
                chunk_count += 1
                if chunk_count == 1:
                    print("   [First audio chunk received]")
                await speaker.play(audio)
            print(f"   [Done - {chunk_count} chunks played]")
        except Exception as e:
            print(f"[TTS Error] {e}")
        finally:
            speaker.close()
    
    keys.stop()
    print("\n👋 Goodbye!")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[Stopped]")
