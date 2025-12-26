"""Speech-to-Text via Cartesia Ink - Streaming Mode."""

import asyncio
import json
import urllib.parse
import websockets
import config


class StreamingSTT:
    """Streaming STT that processes audio chunks and yields transcripts in real-time."""
    
    def __init__(self):
        self.ws = None
        self.audio_queue = asyncio.Queue()
        self.is_finalized = False
    
    async def connect(self):
        """Establish WebSocket connection to Cartesia STT."""
        params = {
            "api_key": config.CARTESIA_API_KEY,
            "cartesia_version": "2024-06-10",
            "model": config.STT_MODEL,
            "language": "en",
            "encoding": "pcm_s16le",
            "sample_rate": str(config.STT_SAMPLE_RATE),
        }
        url = f"wss://api.cartesia.ai/stt/websocket?{urllib.parse.urlencode(params)}"
        self.ws = await websockets.connect(url)
        self.is_finalized = False
        return self
    
    async def feed_audio(self, chunk: bytes):
        """Feed audio chunk to STT - sends immediately."""
        if self.ws and not self.is_finalized:
            await self.ws.send(chunk)
    
    async def finalize(self):
        """Signal end of audio stream."""
        if self.ws and not self.is_finalized:
            self.is_finalized = True
            await self.ws.send(json.dumps({"type": "finalize"}))
    
    async def get_transcript(self) -> str:
        """Get final transcript after finalize."""
        if not self.ws:
            return ""
        
        transcript = ""
        try:
            while True:
                msg = await asyncio.wait_for(self.ws.recv(), timeout=5.0)
                data = json.loads(msg)
                
                if data.get("type") == "transcript":
                    text = data.get("text", "").strip()
                    if text and data.get("is_final", False):
                        transcript = f"{transcript} {text}".strip() if transcript else text
                elif data.get("type") == "done":
                    break
        except asyncio.TimeoutError:
            pass
        
        return transcript.strip()
    
    async def close(self):
        """Close the WebSocket connection."""
        if self.ws:
            await self.ws.close()
            self.ws = None


async def transcribe_streaming(audio_queue: asyncio.Queue) -> str:
    """
    Streaming transcription - consumes audio from queue as it arrives.
    Returns final transcript when audio_queue receives None (end signal).
    """
    params = {
        "api_key": config.CARTESIA_API_KEY,
        "cartesia_version": "2024-06-10",
        "model": config.STT_MODEL,
        "language": "en",
        "encoding": "pcm_s16le",
        "sample_rate": str(config.STT_SAMPLE_RATE),
    }
    url = f"wss://api.cartesia.ai/stt/websocket?{urllib.parse.urlencode(params)}"
    
    async with websockets.connect(url) as ws:
        # Task to send audio chunks as they arrive
        async def sender():
            while True:
                chunk = await audio_queue.get()
                if chunk is None:  # End signal
                    await ws.send(json.dumps({"type": "finalize"}))
                    break
                await ws.send(chunk)
        
        # Start sender in background
        sender_task = asyncio.create_task(sender())
        
        # Collect transcripts
        transcript = ""
        try:
            async for msg in ws:
                data = json.loads(msg)
                if data.get("type") == "transcript":
                    text = data.get("text", "").strip()
                    if text and data.get("is_final", False):
                        transcript = f"{transcript} {text}".strip() if transcript else text
                elif data.get("type") == "done":
                    break
        except Exception:
            pass
        finally:
            sender_task.cancel()
            try:
                await sender_task
            except asyncio.CancelledError:
                pass
        
        return transcript.strip()


# Keep legacy function for compatibility
async def transcribe_audio(audio_bytes: bytes) -> str:
    """Transcribe audio bytes to text (legacy batch mode)."""
    params = {
        "api_key": config.CARTESIA_API_KEY,
        "cartesia_version": "2024-06-10",
        "model": config.STT_MODEL,
        "language": "en",
        "encoding": "pcm_s16le",
        "sample_rate": str(config.STT_SAMPLE_RATE),
    }
    url = f"wss://api.cartesia.ai/stt/websocket?{urllib.parse.urlencode(params)}"
    
    async with websockets.connect(url) as ws:
        # Send audio in chunks
        chunk_size = 1024
        for i in range(0, len(audio_bytes), chunk_size):
            await ws.send(audio_bytes[i:i+chunk_size])
        
        # Finalize
        await ws.send(json.dumps({"type": "finalize"}))
        
        # Receive transcript
        transcript = ""
        try:
            while True:
                msg = await asyncio.wait_for(ws.recv(), timeout=3.0)
                data = json.loads(msg)
                
                if data.get("type") == "transcript":
                    text = data.get("text", "").strip()
                    if text and data.get("is_final", False):
                        transcript = f"{transcript} {text}".strip() if transcript else text
                elif data.get("type") == "done":
                    break
        except asyncio.TimeoutError:
            pass
    
    return transcript.strip()
