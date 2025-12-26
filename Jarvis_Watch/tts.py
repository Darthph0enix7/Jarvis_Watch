"""Text-to-Speech via Cartesia Sonic - Streaming Mode."""

import json
import base64
import uuid
import asyncio
import urllib.parse
import websockets
import config


class StreamingTTS:
    """
    Streaming TTS that maintains a persistent connection and sends text incrementally.
    Yields audio chunks as soon as they're ready - minimizes time-to-first-audio.
    """
    
    def __init__(self):
        self.ws = None
        self.context_id = None
    
    async def connect(self):
        """Establish WebSocket connection to Cartesia TTS."""
        params = {
            "api_key": config.CARTESIA_API_KEY,
            "cartesia_version": "2024-06-10"
        }
        url = f"wss://api.cartesia.ai/tts/websocket?{urllib.parse.urlencode(params)}"
        self.ws = await websockets.connect(url)
        self.context_id = str(uuid.uuid4())
        return self
    
    async def synthesize_chunk(self, text: str, is_last: bool = False):
        """
        Send text chunk for synthesis and yield audio as it arrives.
        Use continue=True for all but the last chunk to maintain prosody.
        """
        if not self.ws or not text.strip():
            return
        
        req = {
            "model_id": config.TTS_MODEL,
            "transcript": text,
            "voice": {"mode": "id", "id": config.VOICE_ID},
            "output_format": {
                "container": "raw",
                "encoding": "pcm_s16le",
                "sample_rate": config.TTS_SAMPLE_RATE
            },
            "context_id": self.context_id,
            "continue": not is_last  # Keep context open for smoother prosody
        }
        await self.ws.send(json.dumps(req))
        
        # Yield audio chunks as they arrive
        async for msg in self.ws:
            data = json.loads(msg)
            if data.get("type") == "chunk":
                audio = data.get("data", "")
                if audio:
                    yield base64.b64decode(audio)
            elif data.get("type") in ("done", "error"):
                break
    
    async def close(self):
        """Close the WebSocket connection."""
        if self.ws:
            await self.ws.close()
            self.ws = None


async def tts_stream(text_iterator):
    """
    Streaming TTS pipeline - consumes text chunks and yields audio immediately.
    
    OPTIMIZATION: Buffers ~5-8 words or until punctuation before sending to TTS.
    This balances latency (smaller chunks = faster first audio) vs quality 
    (larger chunks = better prosody).
    """
    params = {
        "api_key": config.CARTESIA_API_KEY,
        "cartesia_version": "2024-06-10"
    }
    url = f"wss://api.cartesia.ai/tts/websocket?{urllib.parse.urlencode(params)}"
    
    async with websockets.connect(url) as ws:
        context_id = str(uuid.uuid4())
        text_buffer = ""
        chunk_count = 0
        
        async def send_to_tts(text: str, is_last: bool = False):
            """Send text to TTS and yield audio chunks."""
            nonlocal chunk_count
            if not text.strip():
                return
            
            req = {
                "model_id": config.TTS_MODEL,
                "transcript": text,
                "voice": {"mode": "id", "id": config.VOICE_ID},
                "output_format": {
                    "container": "raw",
                    "encoding": "pcm_s16le",
                    "sample_rate": config.TTS_SAMPLE_RATE
                },
                "context_id": context_id,
                "continue": not is_last
            }
            await ws.send(json.dumps(req))
            chunk_count += 1
            
            # Read all audio for this chunk
            async for msg in ws:
                data = json.loads(msg)
                if data.get("type") == "chunk":
                    audio = data.get("data", "")
                    if audio:
                        yield base64.b64decode(audio)
                elif data.get("type") in ("done", "error"):
                    break
        
        # Process incoming text chunks
        async for text_chunk in text_iterator:
            if text_chunk is None:  # End signal
                break
            
            text_buffer += text_chunk
            
            # Check if we should send to TTS now
            # Strategy: Send on sentence boundaries or every ~6 words
            should_send = False
            send_text = ""
            
            # Check for sentence-ending punctuation
            for delim in ['. ', '! ', '? ', '.\n', '!\n', '?\n']:
                if delim in text_buffer:
                    parts = text_buffer.split(delim, 1)
                    send_text = parts[0].strip() + delim.strip()
                    text_buffer = parts[1] if len(parts) > 1 else ""
                    should_send = True
                    break
            
            # Check for comma with enough content (natural pause point)
            if not should_send and ', ' in text_buffer:
                parts = text_buffer.split(', ', 1)
                if len(parts[0].split()) >= 4:  # At least 4 words before comma
                    send_text = parts[0].strip() + ','
                    text_buffer = parts[1] if len(parts) > 1 else ""
                    should_send = True
            
            # Fallback: Send if buffer has ~8+ words
            if not should_send and len(text_buffer.split()) >= 8:
                words = text_buffer.split()
                send_text = ' '.join(words[:6])
                text_buffer = ' '.join(words[6:])
                should_send = True
            
            if should_send and send_text:
                async for audio in send_to_tts(send_text, is_last=False):
                    yield audio
        
        # Flush remaining buffer
        if text_buffer.strip():
            async for audio in send_to_tts(text_buffer.strip(), is_last=True):
                yield audio


# Keep original function for compatibility
async def text_to_speech(text: str):
    """Convert text to speech audio chunks (legacy batch mode)."""
    params = {
        "api_key": config.CARTESIA_API_KEY,
        "cartesia_version": "2024-06-10"
    }
    url = f"wss://api.cartesia.ai/tts/websocket?{urllib.parse.urlencode(params)}"
    
    async with websockets.connect(url) as ws:
        req = {
            "model_id": config.TTS_MODEL,
            "transcript": text,
            "voice": {"mode": "id", "id": config.VOICE_ID},
            "output_format": {
                "container": "raw",
                "encoding": "pcm_s16le",
                "sample_rate": config.TTS_SAMPLE_RATE
            },
            "context_id": str(uuid.uuid4()),
            "continue": False
        }
        await ws.send(json.dumps(req))
        
        async for msg in ws:
            data = json.loads(msg)
            if data.get("type") == "chunk":
                audio = data.get("data", "")
                if audio:
                    yield base64.b64decode(audio)
            elif data.get("type") in ("done", "error"):
                break
