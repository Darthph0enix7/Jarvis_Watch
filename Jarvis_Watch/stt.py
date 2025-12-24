"""Speech-to-Text via Cartesia Ink."""

import asyncio
import json
import urllib.parse
import websockets
import config

async def transcribe_audio(audio_bytes: bytes) -> str:
    """Transcribe audio bytes to text."""
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
        # Send audio
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
