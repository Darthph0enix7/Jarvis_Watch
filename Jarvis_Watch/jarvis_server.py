#!/usr/bin/env python3
"""Server - Orchestrates STT → LLM → TTS pipeline."""

import asyncio
import json
import logging
import argparse
import websockets

from config import get_config
from stt import STT
from tts import TTS
from llm import LLM

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

class VoiceServer:
    def __init__(self):
        self.cfg = get_config()
        self.stt = STT()
        self.tts = TTS()
        self.llm = LLM()
    
    async def handle_client(self, ws):
        log.info("📱 Client connected")
        
        try:
            async for msg in ws:
                if isinstance(msg, bytes):
                    continue  # Ignore stray audio
                
                try:
                    data = json.loads(msg)
                    msg_type = data.get("type")
                    
                    if msg_type == "voice_request":
                        await self._process_voice_request(ws, data)
                    
                except json.JSONDecodeError:
                    pass
        
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            log.info("📱 Client disconnected")
    
    async def _process_voice_request(self, ws, data):
        """Process complete voice request: audio → STT → LLM → TTS → audio."""
        audio_data = data.get("audio", [])
        if not audio_data:
            return
        
        # Combine audio chunks
        combined_audio = b''.join(bytes(chunk) for chunk in audio_data)
        log.info(f"🎤 Received {len(combined_audio)} bytes audio")
        
        # STT: Convert audio to text
        transcript = await self._run_stt(combined_audio)
        if not transcript:
            await ws.send(json.dumps({"type": "error", "message": "No speech detected"}))
            return
        
        log.info(f"📝 Transcript: {transcript}")
        await ws.send(json.dumps({"type": "transcript", "text": transcript}))
        
        # LLM → TTS: Stream response
        await ws.send(json.dumps({"type": "response_start"}))
        
        full_response = ""
        async for text_chunk in self.llm.stream_response(transcript):
            full_response += text_chunk
            await ws.send(json.dumps({"type": "response_text", "text": text_chunk}))
        
        log.info(f"🤖 Response: {full_response[:50]}...")
        
        # TTS: Convert response to audio and stream
        await ws.send(json.dumps({"type": "audio_start", "sample_rate": self.cfg.tts.sample_rate}))
        
        chunk_count = 0
        async for audio_chunk in self.tts.stream(full_response):
            await ws.send(audio_chunk)
            chunk_count += 1
            await asyncio.sleep(0.01)  # Pace the stream
        
        await ws.send(json.dumps({"type": "audio_end"}))
        log.info(f"🔊 Sent {chunk_count} audio chunks")
    
    async def _run_stt(self, audio_bytes: bytes) -> str:
        """Run STT on audio bytes."""
        try:
            return await self.stt.transcribe_audio(audio_bytes)
        except Exception as e:
            log.error(f"STT error: {e}")
            return ""
    
    async def run(self, host: str = None, port: int = None):
        host = host or self.cfg.server.host
        port = port or self.cfg.server.port
        log.info(f"🖥️ Server starting on ws://{host}:{port}")
        
        async with websockets.serve(self.handle_client, host, port):
            await asyncio.Future()


async def main():
    parser = argparse.ArgumentParser(description="Jarvis Voice Server")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", "-p", type=int, default=None)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    server = VoiceServer()
    await server.run(args.host, args.port)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Server stopped")
