"""Text-to-Speech - Cartesia Sonic streaming."""

import asyncio
import json
import base64
import uuid
import urllib.parse
import websockets
from config import get_config

class TTS:
    def __init__(self):
        cfg = get_config()
        params = {
            "api_key": cfg.cartesia_api_key,
            "cartesia_version": "2024-06-10"
        }
        self.url = f"wss://api.cartesia.ai/tts/websocket?{urllib.parse.urlencode(params)}"
        self.model = cfg.tts.model
        self.voice_id = cfg.tts.voice_id
        self.sample_rate = cfg.tts.sample_rate
        self.encoding = cfg.tts.encoding
        self.max_chunk = cfg.tts.chunk_max_chars
    
    async def stream(self, text: str):
        """Yields raw PCM audio bytes."""
        if not text.strip():
            return
        
        async with websockets.connect(self.url) as ws:
            context_id = str(uuid.uuid4())
            chunks = self._split(text)
            
            async def sender():
                for i, chunk in enumerate(chunks):
                    is_last = (i == len(chunks) - 1)
                    req = {
                        "model_id": self.model,
                        "transcript": chunk,
                        "voice": {"mode": "id", "id": self.voice_id},
                        "output_format": {
                            "container": "raw",
                            "encoding": self.encoding,
                            "sample_rate": self.sample_rate
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
                    elif data.get("type") in ("done", "error"):
                        break
            
            sender_task = asyncio.create_task(sender())
            try:
                async for audio in receiver():
                    yield audio
            finally:
                sender_task.cancel()
    
    async def stream_text_chunks(self, text_gen):
        """Stream TTS from a text generator (for LLM streaming)."""
        async with websockets.connect(self.url) as ws:
            context_id = str(uuid.uuid4())
            buffer = ""
            chunk_count = 0
            
            async def process_text():
                nonlocal buffer, chunk_count
                async for text_chunk in text_gen:
                    buffer += text_chunk
                    sentences = self._extract_sentences(buffer)
                    for sentence in sentences:
                        if sentence.strip():
                            chunk_count += 1
                            is_last = False
                            req = {
                                "model_id": self.model,
                                "transcript": sentence,
                                "voice": {"mode": "id", "id": self.voice_id},
                                "output_format": {
                                    "container": "raw",
                                    "encoding": self.encoding,
                                    "sample_rate": self.sample_rate
                                },
                                "context_id": context_id,
                                "continue": True
                            }
                            await ws.send(json.dumps(req))
                    buffer = sentences[-1] if sentences else buffer
                
                # Send remaining buffer
                if buffer.strip():
                    req = {
                        "model_id": self.model,
                        "transcript": buffer,
                        "voice": {"mode": "id", "id": self.voice_id},
                        "output_format": {
                            "container": "raw",
                            "encoding": self.encoding,
                            "sample_rate": self.sample_rate
                        },
                        "context_id": context_id,
                        "continue": False
                    }
                    await ws.send(json.dumps(req))
            
            async def receiver():
                async for msg in ws:
                    data = json.loads(msg)
                    if data.get("type") == "chunk":
                        audio = data.get("data", "")
                        if audio:
                            yield base64.b64decode(audio)
                    elif data.get("type") in ("done", "error"):
                        break
            
            sender_task = asyncio.create_task(process_text())
            try:
                async for audio in receiver():
                    yield audio
            finally:
                sender_task.cancel()
    
    def _split(self, text: str) -> list[str]:
        if len(text) <= self.max_chunk:
            return [text]
        parts = text.replace("!", "!|").replace("?", "?|").replace(".", ".|").split("|")
        chunks, cur = [], ""
        for p in parts:
            p = p.strip()
            if not p:
                continue
            if len(cur) + len(p) <= self.max_chunk:
                cur = f"{cur} {p}".strip()
            else:
                if cur:
                    chunks.append(cur)
                cur = p
        if cur:
            chunks.append(cur)
        return chunks or [text]
    
    def _extract_sentences(self, text: str) -> list[str]:
        """Extract complete sentences, return remaining as last element."""
        for delim in [". ", "! ", "? "]:
            if delim in text:
                parts = text.split(delim)
                complete = [p + delim.strip() for p in parts[:-1] if p.strip()]
                complete.append(parts[-1])
                return complete
        return [text]
