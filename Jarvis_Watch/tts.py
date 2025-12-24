"""Text-to-Speech via Cartesia Sonic."""

import json
import base64
import uuid
import urllib.parse
import websockets
import config

async def text_to_speech(text: str):
    """Convert text to speech audio chunks."""
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
