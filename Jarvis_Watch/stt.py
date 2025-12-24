"""Speech-to-Text - Cartesia Ink streaming."""

import asyncio
import json
import urllib.parse
import websockets
from config import get_config

class STT:
    def __init__(self):
        cfg = get_config()
        params = {
            "api_key": cfg.cartesia_api_key,
            "cartesia_version": "2024-06-10",
            "model": cfg.stt.model,
            "language": cfg.stt.language,
            "encoding": cfg.stt.encoding,
            "sample_rate": str(cfg.stt.sample_rate),
            "min_volume": str(cfg.stt.min_volume),
            "max_silence_duration_secs": str(cfg.stt.max_silence_duration)
        }
        self.url = f"wss://api.cartesia.ai/stt/websocket?{urllib.parse.urlencode(params)}"
    
    async def transcribe_audio(self, audio_bytes: bytes) -> str:
        """Transcribe audio bytes, return final text."""
        accumulated = ""
        
        async with websockets.connect(self.url) as ws:
            # Send all audio
            chunk_size = 1024
            for i in range(0, len(audio_bytes), chunk_size):
                await ws.send(audio_bytes[i:i+chunk_size])
            
            # Send finalize
            await ws.send(json.dumps({"type": "finalize"}))
            
            # Receive transcripts until done
            try:
                async for msg in ws:
                    data = json.loads(msg)
                    msg_type = data.get("type")
                    
                    if msg_type == "transcript":
                        text = data.get("text", "").strip()
                        is_final = data.get("is_final", False)
                        if text and is_final:
                            accumulated = f"{accumulated} {text}".strip() if accumulated else text
                    
                    elif msg_type == "done":
                        break
                    
                    elif msg_type == "error":
                        break
            except asyncio.TimeoutError:
                pass
        
        return accumulated.strip()
    
    async def transcribe(self, audio_gen, stop_event: asyncio.Event = None):
        """Yields (text, is_final, accumulated_text) tuples. For streaming use."""
        accumulated = ""
        
        async with websockets.connect(self.url) as ws:
            send_done = asyncio.Event()
            
            async def sender():
                try:
                    async for chunk in audio_gen:
                        if stop_event and stop_event.is_set():
                            break
                        await ws.send(chunk)
                except:
                    pass
                finally:
                    try:
                        await ws.send(json.dumps({"type": "finalize"}))
                    except:
                        pass
                    send_done.set()
            
            async def receiver():
                nonlocal accumulated
                try:
                    async for msg in ws:
                        if stop_event and stop_event.is_set():
                            break
                        data = json.loads(msg)
                        msg_type = data.get("type")
                        
                        if msg_type == "transcript":
                            text = data.get("text", "").strip()
                            is_final = data.get("is_final", False)
                            if text and is_final:
                                accumulated = f"{accumulated} {text}".strip() if accumulated else text
                            if text:
                                yield (text, is_final, accumulated)
                        
                        elif msg_type == "done":
                            break
                except:
                    pass
            
            sender_task = asyncio.create_task(sender())
            try:
                async for result in receiver():
                    yield result
            finally:
                sender_task.cancel()
