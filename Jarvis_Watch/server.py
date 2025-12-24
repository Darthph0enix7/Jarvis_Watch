#!/usr/bin/env python3
"""Demo Server - Echoes audio back with mock transcription."""

import asyncio
import json
import logging
import argparse

import websockets

# =============================================================================
# Config
# =============================================================================

HOST = "localhost"
PORT = 8765

# =============================================================================
# Server
# =============================================================================

async def handle_client(ws):
    """Handle a single client connection."""
    logging.info("📱 Client connected")
    audio_chunks = []
    sample_rate = 16000
    
    try:
        async for msg in ws:
            if isinstance(msg, bytes):
                audio_chunks.append(msg)
            else:
                try:
                    data = json.loads(msg)
                    msg_type = data.get("type")
                    
                    if msg_type == "stream_start":
                        audio_chunks = []
                        sample_rate = data.get("rate", 16000)
                        logging.info(f"🎤 Stream started (rate: {sample_rate})")
                        await ws.send(json.dumps({"type": "stream_ready"}))
                    
                    elif msg_type == "stream_end":
                        logging.info(f"🔇 Stream ended ({len(audio_chunks)} chunks)")
                        
                        # Send transcription
                        await ws.send(json.dumps({
                            "type": "transcription",
                            "text": f"[Received {len(audio_chunks)} audio chunks]",
                            "is_final": True
                        }))
                        
                        # Send response
                        await asyncio.sleep(0.2)
                        await ws.send(json.dumps({
                            "type": "response",
                            "text": "Echo playback starting..."
                        }))
                        
                        # Echo audio back (combine all chunks for smooth playback)
                        if audio_chunks:
                            combined = b''.join(audio_chunks)
                            # Send in consistent chunk sizes
                            chunk_size = 1024  # bytes
                            for i in range(0, len(combined), chunk_size):
                                await ws.send(combined[i:i+chunk_size])
                                await asyncio.sleep(chunk_size / (sample_rate * 2) * 0.9)
                        
                        audio_chunks = []
                        logging.info("✓ Echo complete")
                        
                except json.JSONDecodeError:
                    pass
    
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        logging.info("📱 Client disconnected")

async def run_server(host: str = HOST, port: int = PORT):
    """Start the WebSocket server."""
    logging.info(f"🖥️ Server starting on ws://{host}:{port}")
    async with websockets.serve(handle_client, host, port):
        await asyncio.Future()

# =============================================================================
# Main
# =============================================================================

async def main():
    parser = argparse.ArgumentParser(description="Demo Server")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", "-p", type=int, default=PORT)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(message)s", datefmt="%H:%M:%S"
    )
    
    await run_server(args.host, args.port)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Server stopped")
