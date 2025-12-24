#!/usr/bin/env python3
"""TTS Demo - Cartesia Sonic 3 with chunked streaming for low latency."""

import asyncio
import websockets
import json
import pyaudio
import urllib.parse
import uuid
import base64

# =============================================================================
# Config
# =============================================================================

API_KEY = "sk_car_eCzTSfNDxYxhgteweyBYr3"
VOICE_ID = "ffe42012-140d-40ab-8cc3-d3f0e957dbc9"
MODEL = "sonic-3"
SAMPLE_RATE = 24000  # TTS output rate
CHANNELS = 1

# =============================================================================
# TTS Provider
# =============================================================================

class CartesiaTTS:
    def __init__(self, api_key: str, voice_id: str, model: str = "sonic-3"):
        self.api_key = api_key
        self.voice_id = voice_id
        self.model = model
        
        params = {
            "api_key": api_key,
            "cartesia_version": "2024-06-10"
        }
        query = urllib.parse.urlencode(params)
        self.url = f"wss://api.cartesia.ai/tts/websocket?{query}"
    
    async def stream_tts(self, text: str):
        """Stream TTS audio chunks. Yields raw PCM bytes."""
        
        async with websockets.connect(self.url) as ws:
            context_id = str(uuid.uuid4())
            
            # Split text into chunks for faster initial response
            chunks = self._split_text(text)
            
            async def sender():
                for i, chunk in enumerate(chunks):
                    is_last = (i == len(chunks) - 1)
                    
                    request = {
                        "model_id": self.model,
                        "transcript": chunk,
                        "voice": {
                            "mode": "id",
                            "id": self.voice_id
                        },
                        "output_format": {
                            "container": "raw",
                            "encoding": "pcm_s16le",
                            "sample_rate": SAMPLE_RATE
                        },
                        "context_id": context_id,
                        "continue": not is_last  # Continue context until last chunk
                    }
                    
                    await ws.send(json.dumps(request))
                    
                    # Small delay between chunks to not overwhelm
                    if not is_last:
                        await asyncio.sleep(0.05)
            
            async def receiver():
                try:
                    async for msg in ws:
                        data = json.loads(msg)
                        msg_type = data.get("type")
                        
                        if msg_type == "chunk":
                            # Decode base64 audio data
                            audio_b64 = data.get("data", "")
                            if audio_b64:
                                audio_bytes = base64.b64decode(audio_b64)
                                yield audio_bytes
                        
                        elif msg_type == "done":
                            break
                        
                        elif msg_type == "error":
                            print(f"\n[TTS Error] {data.get('message', data)}")
                            break
                            
                except Exception as e:
                    print(f"\n[Receiver Error] {e}")
            
            # Start sender in background
            sender_task = asyncio.create_task(sender())
            
            try:
                async for audio_chunk in receiver():
                    yield audio_chunk
            finally:
                sender_task.cancel()
                try:
                    await sender_task
                except asyncio.CancelledError:
                    pass
    
    def _split_text(self, text: str, max_chunk_size: int = 100) -> list[str]:
        """Split text into chunks at sentence boundaries for streaming."""
        if len(text) <= max_chunk_size:
            return [text]
        
        chunks = []
        sentences = text.replace("!", "!|").replace("?", "?|").replace(".", ".|").split("|")
        
        current_chunk = ""
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            
            if len(current_chunk) + len(sentence) <= max_chunk_size:
                current_chunk += " " + sentence if current_chunk else sentence
            else:
                if current_chunk:
                    chunks.append(current_chunk)
                current_chunk = sentence
        
        if current_chunk:
            chunks.append(current_chunk)
        
        # If we still have long chunks, just return original
        return chunks if chunks else [text]

# =============================================================================
# Audio Playback with Queue
# =============================================================================

class AudioPlayer:
    def __init__(self, sample_rate: int = SAMPLE_RATE):
        self.sample_rate = sample_rate
        self.pa = pyaudio.PyAudio()
        self.stream = None
        self.queue = asyncio.Queue()
        self._playing = False
    
    def start(self):
        self.stream = self.pa.open(
            format=pyaudio.paInt16,
            channels=CHANNELS,
            rate=self.sample_rate,
            output=True,
            frames_per_buffer=1024
        )
        self._playing = True
    
    def stop(self):
        self._playing = False
        if self.stream:
            self.stream.stop_stream()
            self.stream.close()
        self.pa.terminate()
    
    async def play_chunk(self, chunk: bytes):
        """Play audio chunk synchronously."""
        if self.stream and self._playing:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self.stream.write, chunk)

# =============================================================================
# Main Demo
# =============================================================================

async def main():
    tts = CartesiaTTS(API_KEY, VOICE_ID, MODEL)
    player = AudioPlayer()
    
    # Test text - you can change this
    test_text = input("Enter text to speak (or press Enter for demo): ").strip()
    if not test_text:
        test_text = "Hello! This is a test of the Cartesia text to speech system. It streams audio in chunks for ultra low latency. Pretty cool, right?"
    
    print(f"\n[Speaking] {test_text[:50]}...")
    print("=" * 60)
    
    player.start()
    
    try:
        chunk_count = 0
        async for audio_chunk in tts.stream_tts(test_text):
            chunk_count += 1
            if chunk_count == 1:
                print("[First audio chunk received!]")
            await player.play_chunk(audio_chunk)
        
        print(f"\n[Done] Played {chunk_count} audio chunks")
        
    except Exception as e:
        print(f"\n[Error] {e}")
    finally:
        player.stop()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[Stopped]")
