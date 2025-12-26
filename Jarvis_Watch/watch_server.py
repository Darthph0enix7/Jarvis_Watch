#!/usr/bin/env python3
"""Watch Server - Streaming Pipeline: STT → LLM → TTS with minimal latency."""

import asyncio
import json
import logging
import time
import websockets

import config
from stt import StreamingSTT
from tts import tts_stream
import llm

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)


async def handle_client(ws):
    """Handle client connection with streaming pipeline."""
    log.info("📱 Client connected")
    
    try:
        stt_client = None
        audio_received = False
        
        async for msg in ws:
            if isinstance(msg, bytes):
                # Audio chunk - forward to STT immediately
                if stt_client and audio_received:
                    await stt_client.feed_audio(msg)
            else:
                data = json.loads(msg)
                msg_type = data.get("type")
                
                if msg_type == "audio_start":
                    # Initialize streaming STT
                    stt_client = StreamingSTT()
                    await stt_client.connect()
                    audio_received = True
                    log.info("🎤 Streaming audio started")
                
                elif msg_type == "audio_end":
                    if stt_client and audio_received:
                        audio_received = False
                        await process_streaming(ws, stt_client)
                        await stt_client.close()
                        stt_client = None
    
    except websockets.exceptions.ConnectionClosed:
        pass
    except Exception as e:
        log.error(f"Client handler error: {e}")
    finally:
        log.info("📱 Client disconnected")


async def process_streaming(ws, stt_client: StreamingSTT):
    """
    Streaming pipeline: STT → LLM → TTS → Client
    
    Key optimization: Each stage starts as soon as it has input,
    not waiting for previous stage to complete.
    """
    timings = {
        "start": time.time(),
        "stt_done": None,
        "llm_first": None,
        "tts_first": None,
        "audio_first": None,
        "complete": None
    }
    
    # Step 1: Finalize STT and get transcript
    await stt_client.finalize()
    transcript = await stt_client.get_transcript()
    timings["stt_done"] = time.time()
    
    stt_time = (timings["stt_done"] - timings["start"]) * 1000
    log.info(f"📝 [{stt_time:.0f}ms] {transcript}")
    
    if not transcript:
        await ws.send(json.dumps({"type": "error", "message": "No speech detected"}))
        return
    
    await ws.send(json.dumps({"type": "transcript", "text": transcript}))
    
    # Step 2: Create streaming pipeline LLM → TTS → Client
    # Using async queues to connect the stages
    
    text_queue = asyncio.Queue()
    audio_queue = asyncio.Queue()
    
    async def llm_to_queue():
        """Stream LLM output to text queue."""
        try:
            async for chunk in llm.generate_response(transcript):
                if timings["llm_first"] is None:
                    timings["llm_first"] = time.time()
                # Send text to client for display
                await ws.send(json.dumps({"type": "response_text", "text": chunk}))
                # Also queue for TTS
                await text_queue.put(chunk)
            await text_queue.put(None)  # Signal end
        except Exception as e:
            log.error(f"LLM error: {e}")
            await text_queue.put(None)
    
    async def queue_to_text_iterator():
        """Convert queue to async iterator for TTS."""
        while True:
            chunk = await text_queue.get()
            if chunk is None:
                break
            yield chunk
    
    async def tts_to_queue():
        """Stream TTS output to audio queue."""
        try:
            async for audio_chunk in tts_stream(queue_to_text_iterator()):
                if timings["tts_first"] is None:
                    timings["tts_first"] = time.time()
                await audio_queue.put(audio_chunk)
            await audio_queue.put(None)  # Signal end
        except Exception as e:
            log.error(f"TTS error: {e}")
            await audio_queue.put(None)
    
    async def send_audio():
        """Stream audio to client immediately."""
        chunk_count = 0
        try:
            while True:
                audio = await audio_queue.get()
                if audio is None:
                    break
                if timings["audio_first"] is None:
                    timings["audio_first"] = time.time()
                await ws.send(audio)
                chunk_count += 1
        except Exception as e:
            log.error(f"Audio sender error: {e}")
        return chunk_count
    
    # Run all three stages in parallel
    tasks = await asyncio.gather(
        llm_to_queue(),
        tts_to_queue(),
        send_audio(),
        return_exceptions=True
    )
    
    chunk_count = tasks[2] if isinstance(tasks[2], int) else 0
    timings["complete"] = time.time()
    
    # Signal end of audio
    await ws.send(json.dumps({"type": "audio_end"}))
    
    # Log metrics
    total = (timings["complete"] - timings["start"]) * 1000
    stt_ms = (timings["stt_done"] - timings["start"]) * 1000
    llm_first_ms = ((timings["llm_first"] - timings["stt_done"]) * 1000) if timings["llm_first"] else 0
    tts_first_ms = ((timings["tts_first"] - timings["stt_done"]) * 1000) if timings["tts_first"] else 0
    audio_first_ms = ((timings["audio_first"] - timings["stt_done"]) * 1000) if timings["audio_first"] else 0
    
    log.info(f"📊 Pipeline Timings:")
    log.info(f"   STT complete: {stt_ms:.0f}ms")
    log.info(f"   LLM first token: +{llm_first_ms:.0f}ms")
    log.info(f"   TTS first chunk: +{tts_first_ms:.0f}ms")
    log.info(f"   First audio sent: +{audio_first_ms:.0f}ms (← KEY LATENCY)")
    log.info(f"   Total: {total:.0f}ms | {chunk_count} audio chunks")


async def main():
    log.info(f"🖥️  Server starting on ws://{config.SERVER_HOST}:{config.SERVER_PORT}")
    log.info("   Streaming mode enabled - minimal latency pipeline")
    
    async with websockets.serve(handle_client, config.SERVER_HOST, config.SERVER_PORT):
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Server stopped")
