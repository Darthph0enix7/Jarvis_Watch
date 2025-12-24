#!/usr/bin/env python3
"""Jarvis Demo - Simple voice assistant: Mic → STT → LLM → TTS → Speaker"""

import asyncio
import json
import base64
import uuid
import urllib.parse
import os
import time

import pyaudio
import websockets
import numpy as np

# =============================================================================
# Config
# =============================================================================

API_KEY = os.environ.get("CARTESIA_API_KEY", "sk_car_eCzTSfNDxYxhgteweyBYr3")
VOICE_ID = "ffe42012-140d-40ab-8cc3-d3f0e957dbc9"

STT_RATE = 16000
TTS_RATE = 24000
CHUNK = 512

# VAD settings
SILENCE_THRESHOLD = 0.01  # Energy threshold
SILENCE_DURATION = 1.5    # Seconds of silence to end recording

# =============================================================================
# Simple Energy VAD
# =============================================================================

def get_energy(audio_bytes: bytes) -> float:
    audio = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    return np.sqrt(np.mean(audio ** 2))

# =============================================================================
# STT - Real-time streaming
# =============================================================================

async def stream_stt(audio_queue: asyncio.Queue, result_queue: asyncio.Queue):
    """Stream audio to Cartesia STT, put transcripts in result_queue."""
    params = {
        "api_key": API_KEY,
        "cartesia_version": "2024-06-10",
        "model": "ink-whisper",
        "language": "en",
        "encoding": "pcm_s16le",
        "sample_rate": str(STT_RATE),
    }
    url = f"wss://api.cartesia.ai/stt/websocket?{urllib.parse.urlencode(params)}"
    
    async with websockets.connect(url) as ws:
        sender_done = asyncio.Event()
        
        async def sender():
            try:
                while True:
                    item = await audio_queue.get()
                    if item is None:  # Stop signal
                        await ws.send(json.dumps({"type": "finalize"}))
                        break
                    await ws.send(item)
            except Exception as e:
                print(f"\n[Sender Error: {e}]")
            finally:
                sender_done.set()
        
        async def receiver():
            transcript = ""
            try:
                while True:
                    try:
                        # Longer timeout, but break if sender is done and we've waited a bit
                        msg = await asyncio.wait_for(ws.recv(), timeout=2.0)
                        data = json.loads(msg)
                        
                        if data.get("type") == "transcript":
                            text = data.get("text", "").strip()
                            is_final = data.get("is_final", False)
                            if text:
                                if is_final:
                                    transcript = f"{transcript} {text}".strip() if transcript else text
                                    print(f"\r📝 {transcript}", end="", flush=True)
                                else:
                                    print(f"\r📝 {transcript} \033[90m{text}\033[0m   ", end="", flush=True)
                        elif data.get("type") == "done":
                            break
                    except asyncio.TimeoutError:
                        # Only break if sender is done
                        if sender_done.is_set():
                            break
            except Exception as e:
                print(f"\n[STT Error: {e}]")
            finally:
                await result_queue.put(transcript)
        
        sender_task = asyncio.create_task(sender())
        try:
            await receiver()
        finally:
            sender_task.cancel()
            try:
                await sender_task
            except asyncio.CancelledError:
                pass

# =============================================================================
# LLM - Mock streaming
# =============================================================================

async def stream_llm(user_input: str):
    """Yield LLM response chunks."""
    # Mock responses
    responses = {
        "hello": "Hello! I'm Jarvis. How can I help you today?",
        "time": "I don't have access to the current time, but your watch should display it.",
        "weather": "It looks like a beautiful day outside. Perfect for a walk!",
        "name": "I'm Jarvis, your voice assistant.",
        "help": "I can answer questions, set reminders, and help with various tasks.",
    }
    
    lower = user_input.lower()
    response = None
    for key, val in responses.items():
        if key in lower:
            response = val
            break
    
    if not response:
        response = f"I heard: {user_input}. This is a demo response from the mock LLM."
    
    # Stream word by word
    for word in response.split():
        yield word + " "
        await asyncio.sleep(0.05)

# =============================================================================
# TTS - Stream text to audio
# =============================================================================

async def stream_tts(text: str):
    """Yield audio chunks from text."""
    params = {
        "api_key": API_KEY,
        "cartesia_version": "2024-06-10"
    }
    url = f"wss://api.cartesia.ai/tts/websocket?{urllib.parse.urlencode(params)}"
    
    async with websockets.connect(url) as ws:
        req = {
            "model_id": "sonic-3",
            "transcript": text,
            "voice": {"mode": "id", "id": VOICE_ID},
            "output_format": {
                "container": "raw",
                "encoding": "pcm_s16le",
                "sample_rate": TTS_RATE
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

# =============================================================================
# Main
# =============================================================================

async def main():
    print("=" * 60)
    print("🎙️  Jarvis Voice Assistant")
    print("=" * 60)
    print(f"Silence threshold: {SILENCE_THRESHOLD} | Duration: {SILENCE_DURATION}s")
    print("Speak now! I'll listen until you stop talking...")
    print("=" * 60)
    
    # Audio setup
    pa = pyaudio.PyAudio()
    mic = pa.open(format=pyaudio.paInt16, channels=1, rate=STT_RATE,
                  input=True, frames_per_buffer=CHUNK)
    
    audio_queue = asyncio.Queue()
    result_queue = asyncio.Queue()
    
    # Start STT task
    stt_task = asyncio.create_task(stream_stt(audio_queue, result_queue))
    
    # Record with VAD
    loop = asyncio.get_event_loop()
    silence_frames = 0
    speech_started = False
    frames_for_silence = int(SILENCE_DURATION * STT_RATE / CHUNK)
    
    print("\n🔴 Listening...")
    
    while True:
        data = await loop.run_in_executor(None, mic.read, CHUNK, False)
        energy = get_energy(data)
        
        if energy > SILENCE_THRESHOLD:
            speech_started = True
            silence_frames = 0
            await audio_queue.put(data)
        else:
            if speech_started:
                await audio_queue.put(data)  # Include some silence
                silence_frames += 1
                if silence_frames >= frames_for_silence:
                    print("\n\n✓ Speech ended")
                    break
    
    # Stop STT
    await audio_queue.put(None)
    mic.stop_stream()
    mic.close()
    
    # Wait for transcript
    print("⏳ Processing...")
    try:
        transcript = await asyncio.wait_for(result_queue.get(), timeout=8.0)
    except asyncio.TimeoutError:
        print("❌ Timeout waiting for transcript")
        pa.terminate()
        return
    finally:
        stt_task.cancel()
        try:
            await stt_task
        except asyncio.CancelledError:
            pass
    
    if not transcript.strip():
        print("❌ No speech detected")
        pa.terminate()
        return
    
    print(f"\n📝 Final: {transcript}")
    
    # Setup speaker
    spk = pa.open(format=pyaudio.paInt16, channels=1, rate=TTS_RATE,
                  output=True, frames_per_buffer=1024)
    
    # Pipeline queues: LLM → text_queue → TTS → audio_queue → Speaker
    print("\n🤖 Responding...")
    
    text_queue = asyncio.Queue()
    audio_queue = asyncio.Queue()
    
    # Timing metrics
    timings = {
        "start": time.time(),
        "first_text": None,
        "first_audio": None,
        "first_playback": None,
        "chunks_generated": 0,
        "sentences_sent": 0,
    }
    
    async def llm_producer():
        """Generate LLM output and send chunks at natural punctuation boundaries."""
        buffer = ""
        try:
            async for chunk in stream_llm(transcript):
                if timings["first_text"] is None:
                    timings["first_text"] = time.time() - timings["start"]
                
                timings["chunks_generated"] += 1
                print(chunk, end="", flush=True)
                buffer += chunk
                
                # Send at natural punctuation boundaries for context
                # Priority: sentence endings > commas/semicolons
                sent = False
                for delim in ['. ', '! ', '? ', '.\n', '!\n', '?\n']:
                    if delim in buffer:
                        parts = buffer.split(delim, 1)
                        text = parts[0].strip() + delim.strip()
                        if text:
                            timings["sentences_sent"] += 1
                            await text_queue.put(text)
                        buffer = parts[1] if len(parts) > 1 else ""
                        sent = True
                        break
                
                # For longer phrases without sentence endings, split at commas
                if not sent and ', ' in buffer:
                    parts = buffer.split(', ', 1)
                    # Only split if first part is reasonable length (>20 chars)
                    if len(parts[0]) > 20:
                        text = parts[0].strip() + ','
                        timings["sentences_sent"] += 1
                        await text_queue.put(text)
                        buffer = parts[1] if len(parts) > 1 else ""
            
            # Send remaining buffer
            if buffer.strip():
                timings["sentences_sent"] += 1
                await text_queue.put(buffer.strip())
            
            await text_queue.put(None)  # Signal done
        except Exception as e:
            print(f"\n[LLM Error: {e}]")
            await text_queue.put(None)
    
    async def tts_processor():
        """Convert text from queue to audio and put in audio queue."""
        try:
            while True:
                text = await text_queue.get()
                if text is None:  # Done signal
                    await audio_queue.put(None)
                    break
                
                async for audio_chunk in stream_tts(text):
                    if timings["first_audio"] is None:
                        timings["first_audio"] = time.time() - timings["start"]
                    await audio_queue.put(audio_chunk)
        except Exception as e:
            print(f"\n[TTS Error: {e}]")
            await audio_queue.put(None)
    
    async def audio_player():
        """Play audio from queue."""
        try:
            while True:
                audio = await audio_queue.get()
                if audio is None:  # Done signal
                    break
                if timings["first_playback"] is None:
                    timings["first_playback"] = time.time() - timings["start"]
                await loop.run_in_executor(None, spk.write, audio)
        except Exception as e:
            print(f"\n[Player Error: {e}]")
    
    # Run all three in parallel
    await asyncio.gather(
        llm_producer(),
        tts_processor(),
        audio_player()
    )
    
    # Print timing metrics
    total_time = time.time() - timings["start"]
    print("\n")
    print("📊 Timing Metrics:")
    print(f"  ⏱️  Time to first LLM token: {timings['first_text']*1000:.0f}ms")
    print(f"  ⏱️  Time to first audio chunk: {timings['first_audio']*1000:.0f}ms")
    print(f"  ⏱️  Time to first playback: {timings['first_playback']*1000:.0f}ms")
    print(f"  📝 Total LLM chunks: {timings['chunks_generated']}")
    print(f"  📄 Sentences sent to TTS: {timings['sentences_sent']}")
    print(f"  ⏱️  Total time: {total_time:.2f}s")
    if timings['chunks_generated'] > 0:
        avg_chunk = total_time / timings['chunks_generated']
        print(f"  📈 Avg time per chunk: {avg_chunk*1000:.0f}ms")
    
    print("\n✓ Done!")
    
    spk.stop_stream()
    spk.close()
    pa.terminate()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[Stopped]")
