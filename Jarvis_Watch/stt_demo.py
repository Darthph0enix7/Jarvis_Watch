import asyncio
import websockets
import json
import pyaudio
from abc import ABC, abstractmethod
import urllib.parse

# --- 1. Universal STT Interface ---
class STTProvider(ABC):
    @abstractmethod
    async def transcribe_stream(self, audio_generator):
        """Yields text from an incoming audio stream."""
        pass

# --- 2. Cartesia Implementation (URL-Based Config) ---
class CartesiaSTT(STTProvider):
    def __init__(self, api_key):
        self.api_key = api_key
        
        # 1. CONSTRUCT QUERY PARAMS
        # Docs confirm these must be in the URL for raw WebSockets
        params = {
            "api_key": api_key,
            "cartesia_version": "2024-06-10",
            "model": "ink-whisper",
            "language": "en",
            "encoding": "pcm_s16le",
            "sample_rate": "16000",
            "min_volume": "0.1",              # Required for VAD
            "max_silence_duration_secs": "0.7" # Required for endpointing
        }
        
        # Safe URL encoding
        query_string = urllib.parse.urlencode(params)
        self.url = f"wss://api.cartesia.ai/stt/websocket?{query_string}"

    async def transcribe_stream(self, audio_generator):
        print(f"[Debug] Connecting to: {self.url[:60]}...") # Print partial URL for debug
        
        # 2. CONNECT (No headers needed, everything is in URL)
        async with websockets.connect(self.url) as ws:
            print("[Debug] Connected! Streaming audio...")
            
            # NOTE: DO NOT send a JSON config frame here. The URL did that.
            # Start sending audio immediately.

            async def sender():
                try:
                    async for chunk in audio_generator:
                        await ws.send(chunk)
                except Exception as e:
                    print(f"\n[Sender Error] {e}")
                finally:
                    # Send 'finalize' text frame to finish the stream
                    try:
                        await ws.send(json.dumps({"type": "finalize"}))
                    except:
                        pass

            async def receiver():
                try:
                    async for msg in ws:
                        data = json.loads(msg)
                        
                        if data.get("type") == "transcript":
                            text = data.get("text", "").strip()
                            is_final = data.get("is_final", False)
                            if text:
                                yield (text, is_final)
                        
                        elif data.get("type") == "error":
                            print(f"\n[API Error] {data.get('error', data)}")
                except Exception as e:
                    print(f"\n[Receiver Error] {e}")

            # Run sender in background
            sender_task = asyncio.create_task(sender())
            
            try:
                async for text in receiver():
                    yield text
            finally:
                sender_task.cancel()

# --- 3. Audio Input (Mic) ---
async def mic_stream_generator():
    CHUNK = 512 
    RATE = 16000
    p = pyaudio.PyAudio()
    
    stream = p.open(
        format=pyaudio.paInt16, 
        channels=1, 
        rate=RATE, 
        input=True, 
        frames_per_buffer=CHUNK
    )
    
    loop = asyncio.get_event_loop()
    try:
        while True:
            data = await loop.run_in_executor(None, stream.read, CHUNK, False)
            yield data
    finally:
        stream.stop_stream()
        stream.close()
        p.terminate()

# --- 4. Main ---
async def main():
    # REPLACE WITH YOUR KEY
    API_KEY = "sk_car_eCzTSfNDxYxhgteweyBYr3" 
    
    stt = CartesiaSTT(API_KEY)
    
    print(f"Listening to Cartesia Ink... (Ctrl+C to stop)")
    print("=" * 60)
    
    full_transcript = ""
    current_partial = ""
    
    try:
        async for text, is_final in stt.transcribe_stream(mic_stream_generator()):
            if is_final:
                # Final transcript - add to permanent transcript
                if full_transcript and not full_transcript.endswith(" "):
                    full_transcript += " "
                full_transcript += text
                current_partial = ""
                # Display final version
                print(f"\r{full_transcript}", end="", flush=True)
            else:
                # Partial/intermediate - just display temporarily
                current_partial = text
                display = full_transcript
                if display and not display.endswith(" "):
                    display += " "
                display += f"\033[90m{current_partial}\033[0m"  # Gray for partial
                print(f"\r{display}", end="", flush=True)
    except websockets.exceptions.InvalidStatus as e:
        print(f"\n[Connection Rejected] Status: {e.response.status_code}")
        print(f"Reason: {e}")
    except Exception as e:
        print(f"\n[Error] {e}")
    finally:
        if full_transcript:
            print()  # Newline at end

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass