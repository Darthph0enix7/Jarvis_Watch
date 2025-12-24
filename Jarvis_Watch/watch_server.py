#!/usr/bin/env python3
"""Watch Server - Orchestrates STT → LLM → TTS pipeline."""

import asyncio
import json
import logging
import websockets
import time

import config
import stt
import tts
import llm

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

async def handle_client(ws):
    """Handle client connection."""
    log.info("📱 Client connected")
    
    try:
        async for msg in ws:
            data = json.loads(msg)
            
            if data.get("type") == "audio_complete":
                await process_request(ws, data)
    
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        log.info("📱 Client disconnected")

async def process_request(ws, data):
    """Process: Audio → STT → LLM → TTS → Audio."""
    timings = {
        "receive_start": time.time(),
        "stt_start": None,
        "stt_end": None,
        "llm_start": None,
        "llm_first_token": None,
        "tts_first_chunk": None,
        "first_audio_sent": None,
        "complete": None
    }
    
    audio_data = data.get("audio", [])
    if not audio_data:
        return
    
    # Combine audio chunks
    audio_bytes = b''.join(bytes(chunk) for chunk in audio_data)
    log.info(f"🎤 Received {len(audio_bytes)} bytes")
    
    # STT
    timings["stt_start"] = time.time()
    transcript = await stt.transcribe_audio(audio_bytes)
    timings["stt_end"] = time.time()
    stt_time = timings["stt_end"] - timings["stt_start"]
    
    if not transcript:
        await ws.send(json.dumps({"type": "error", "message": "No speech detected"}))
        return
    
    log.info(f"📝 [{stt_time:.2f}s] {transcript}")
    await ws.send(json.dumps({"type": "transcript", "text": transcript}))
    
    # LLM → TTS pipeline
    timings["llm_start"] = time.time()
    await ws.send(json.dumps({"type": "response_start"}))
    
    text_queue = asyncio.Queue()
    audio_queue = asyncio.Queue()
    
    async def llm_producer():
        """Generate LLM text and send at punctuation boundaries."""
        buffer = ""
        try:
            async for chunk in llm.generate_response(transcript):
                if timings["llm_first_token"] is None:
                    timings["llm_first_token"] = time.time()
                await ws.send(json.dumps({"type": "response_text", "text": chunk}))
                buffer += chunk
                
                # Send at sentence or comma boundaries
                sent = False
                for delim in ['. ', '! ', '? ', '.\n', '!\n', '?\n']:
                    if delim in buffer:
                        parts = buffer.split(delim, 1)
                        text = parts[0].strip() + delim.strip()
                        if text:
                            await text_queue.put(text)
                        buffer = parts[1] if len(parts) > 1 else ""
                        sent = True
                        break
                
                if not sent and ', ' in buffer:
                    parts = buffer.split(', ', 1)
                    if len(parts[0]) > 20:
                        await text_queue.put(parts[0].strip() + ',')
                        buffer = parts[1] if len(parts) > 1 else ""
            
            if buffer.strip():
                await text_queue.put(buffer.strip())
            await text_queue.put(None)
        except Exception as e:
            log.error(f"LLM error: {e}")
            await text_queue.put(None)
    
    async def tts_processor():
        """Convert text to audio."""
        try:
            while True:
                text = await text_queue.get()
                if text is None:
                    await audio_queue.put(None)
                    break
                
                async for audio_chunk in tts.text_to_speech(text):
                    if timings["tts_first_chunk"] is None:
                        timings["tts_first_chunk"] = time.time()
                    await audio_queue.put(audio_chunk)
        except Exception as e:
            log.error(f"TTS error: {e}")
            await audio_queue.put(None)
    
    async def audio_sender():
        """Send audio to client."""
        chunk_count = 0
        try:
            while True:
                audio = await audio_queue.get()
                if audio is None:
                    break
                if timings["first_audio_sent"] is None:
                    timings["first_audio_sent"] = time.time()
                await ws.send(audio)
                chunk_count += 1
        except Exception as e:
            log.error(f"Sender error: {e}")
        return chunk_count
    
    await ws.send(json.dumps({"type": "audio_start", "sample_rate": config.TTS_SAMPLE_RATE}))
    
    tasks = await asyncio.gather(
        llm_producer(),
        tts_processor(),
        audio_sender()
    )
    
    chunk_count = tasks[2]
    timings["complete"] = time.time()
    
    await ws.send(json.dumps({"type": "audio_end"}))
    
    # Calculate metrics
    total_time = timings["complete"] - timings["receive_start"]
    stt_time = timings["stt_end"] - timings["stt_start"]
    time_to_first_token = (timings["llm_first_token"] - timings["llm_start"]) if timings["llm_first_token"] else 0
    time_to_first_audio = (timings["tts_first_chunk"] - timings["llm_start"]) if timings["tts_first_chunk"] else 0
    time_to_send = (timings["first_audio_sent"] - timings["llm_start"]) if timings["first_audio_sent"] else 0
    
    log.info(f"📊 Timings:")
    log.info(f"   STT: {stt_time*1000:.0f}ms")
    log.info(f"   LLM first token: {time_to_first_token*1000:.0f}ms")
    log.info(f"   TTS first chunk: {time_to_first_audio*1000:.0f}ms")
    log.info(f"   First audio sent: {time_to_send*1000:.0f}ms")
    log.info(f"   Total: {total_time:.2f}s | {chunk_count} audio chunks")

async def main():
    log.info(f"🖥️  Server starting on ws://{config.SERVER_HOST}:{config.SERVER_PORT}")
    async with websockets.serve(handle_client, config.SERVER_HOST, config.SERVER_PORT):
        await asyncio.Future()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Server stopped")
