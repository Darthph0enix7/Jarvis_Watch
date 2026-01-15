"""
Jarvis Voice Assistant - WebSocket Server

This server handles the voice assistant pipeline:
- Receives audio streams from clients (watch, phone, demo)
- Processes through STT → LLM → TTS pipeline
- Streams audio responses back to clients

Usage:
    python server.py [--host HOST] [--port PORT] [--llm PROVIDER] [--token TOKEN]

Example:
    python server.py --reload --host 0.0.0.0 --port 8000 --llm cerebras    #--token mysecret

Environment Variables:
    JARVIS_AUTH_TOKEN - Authentication token for clients
"""

import asyncio
import json
import sys
import os
import time
import struct
import base64
import argparse
import uuid
import hashlib
import secrets
import signal
import subprocess
from typing import Optional
from urllib.parse import urlencode, parse_qs, urlparse
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path

import websockets
from websockets.server import WebSocketServerProtocol
from websockets.http import Headers
from websockets.asyncio.server import Response
from aiohttp import web
import firebase_admin
from firebase_admin import credentials, messaging

# Import LLM providers
from llm import GeminiLLMClient, CerebrasLLMClient, BaseLLMClient

# Import protocol definitions
from protocol import (
    MessageType, INPUT_SAMPLE_RATE, OUTPUT_SAMPLE_RATE,
    create_message, parse_message, is_binary_audio
)

# ============================================================================
# CONFIGURATION
# ============================================================================

# Authentication
AUTH_TOKEN = os.environ.get("JARVIS_AUTH_TOKEN", "Denemeler123.")

# API Keys (load from environment or use defaults for development)
CARTESIA_API_KEY = os.environ.get("CARTESIA_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
CEREBRAS_API_KEY = os.environ.get("CEREBRAS_API_KEY")

# Statistics storage
STATS_FILE = "session_stats.jsonl"  # JSON Lines format for easy appending

# Firebase configuration
FIREBASE_SERVICE_ACCOUNT = "jarvis-app-43084-firebase-adminsdk-fbsvc-c836619551.json"

# Cartesia endpoints
CARTESIA_STT_URL = "wss://api.cartesia.ai/stt/websocket"
CARTESIA_TTS_URL = "wss://api.cartesia.ai/tts/websocket"

# TTS configuration
TTS_VOICE_ID = "ffe42012-140d-40ab-8cc3-d3f0e957dbc9"
TTS_MODEL = "sonic-3-2025-10-27"
TTS_SPEED = 1.2  # Range: 0.6 to 1.5 (multiplier on default speed)
TTS_VOLUME = 1.5  # Range: 0.5 to 2.0 (multiplier on default volume)
TTS_EMOTION = "neutral"  # Options: neutral, angry, excited, content, sad, scared, etc.

# LLM configuration
GEMINI_MODEL = "models/gemini-2.5-flash-lite"
CEREBRAS_MODEL = "gpt-oss-120b"

# VAD settings
SILENCE_DURATION = 1.0  # seconds
VAD_THRESHOLD = 300  # default RMS threshold

# Cartesia STT parameters
CARTESIA_PARAMS = {
    "api_key": CARTESIA_API_KEY,
    "cartesia_version": "2024-06-10",
    "model": "ink-whisper",
    "language": "en",
    "encoding": "pcm_s16le",
    "sample_rate": str(INPUT_SAMPLE_RATE),
    "min_volume": "0.1",
    "max_silence_duration_secs": "2.0",
}


# ============================================================================
# FIREBASE & DEVICE MANAGEMENT
# ============================================================================

@dataclass
class LocationRecord:
    """Record of a device location update."""
    device_id: str
    device_type: str
    latitude: float
    longitude: float
    accuracy: float
    altitude: Optional[float] = None
    speed: Optional[float] = None
    bearing: Optional[float] = None
    provider: Optional[str] = None
    timestamp: float = field(default_factory=time.time)
    received_at: float = field(default_factory=time.time)
    
    def to_dict(self) -> dict:
        return {
            'device_id': self.device_id,
            'device_type': self.device_type,
            'latitude': self.latitude,
            'longitude': self.longitude,
            'accuracy': self.accuracy,
            'altitude': self.altitude,
            'speed': self.speed,
            'bearing': self.bearing,
            'provider': self.provider,
            'timestamp': self.timestamp,
            'received_at': self.received_at,
            'datetime': datetime.fromtimestamp(self.timestamp).isoformat()
        }


class LocationTracker:
    """Tracks device locations and saves them to file."""
    
    def __init__(self, filepath: str = "device_locations.jsonl"):
        self.filepath = filepath
        self.locations: dict[str, LocationRecord] = {}  # device_id -> latest location
        self.location_history: list[LocationRecord] = []  # All locations
    
    def update_location(self, location: LocationRecord) -> None:
        """Update device location and save to file."""
        self.locations[location.device_id] = location
        self.location_history.append(location)
        self.save_location(location)
    
    def save_location(self, location: LocationRecord) -> None:
        """Append location to JSON Lines file."""
        with open(self.filepath, 'a') as f:
            f.write(json.dumps(location.to_dict()) + '\n')
    
    def get_latest_location(self, device_id: str) -> Optional[LocationRecord]:
        """Get latest location for a device."""
        return self.locations.get(device_id)
    
    def get_all_latest(self) -> dict[str, LocationRecord]:
        """Get latest locations for all devices."""
        return self.locations.copy()


class PeriodicLocationTracker:
    """Periodically requests location from all devices."""
    
    def __init__(self, fcm_service: 'FCMNotificationService', 
                 device_manager: 'DeviceManager',
                 location_tracker: LocationTracker,
                 interval_minutes: int = 30):
        self.fcm_service = fcm_service
        self.device_manager = device_manager
        self.location_tracker = location_tracker
        self.interval_minutes = interval_minutes
        self.is_running = False
        self.task: Optional[asyncio.Task] = None
    
    async def start(self) -> None:
        """Start periodic location tracking."""
        if self.is_running:
            return
        
        self.is_running = True
        self.task = asyncio.create_task(self._run_periodic_task())
        print(f"[LocationTracker] Started periodic tracking (every {self.interval_minutes} minutes)")
    
    async def stop(self) -> None:
        """Stop periodic location tracking."""
        self.is_running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        print("[LocationTracker] Stopped periodic tracking")
    
    async def _run_periodic_task(self) -> None:
        """Main periodic task loop."""
        while self.is_running:
            try:
                await self.request_all_locations()
                # Wait for interval
                await asyncio.sleep(self.interval_minutes * 60)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[LocationTracker] Error in periodic task: {e}")
                await asyncio.sleep(60)  # Wait 1 minute before retry on error
    
    async def request_all_locations(self) -> None:
        """Request location from all active devices."""
        device_ids = list(self.device_manager.devices.keys())
        active_devices = [did for did in device_ids 
                         if self.device_manager.devices[did]['is_active']]
        
        if not active_devices:
            total_devices = len(self.device_manager.devices)
            if total_devices == 0:
                print("[LocationTracker] ⚠️  No devices registered yet")
                print("[LocationTracker]    Devices must POST to /api/register-device first")
                print("[LocationTracker]    Check Android app FCM initialization logs\n")
            else:
                print(f"[LocationTracker] ⚠️  {total_devices} devices registered but all inactive")
                for did, info in self.device_manager.devices.items():
                    print(f"[LocationTracker]    - {did}: is_active={info['is_active']}\n")
            return
        
        print(f"\n[LocationTracker] Requesting location from {len(active_devices)} devices...")
        
        for device_id in active_devices:
            try:
                device_info = self.device_manager.devices[device_id]
                device_type = device_info['device_type']
                
                # Check if device recently responded (online status)
                last_seen_str = device_info.get('last_seen')
                if last_seen_str:
                    last_seen = datetime.fromisoformat(last_seen_str)
                    time_since_seen = (datetime.now() - last_seen).total_seconds()
                    
                    # If not seen in 5 minutes, consider offline
                    if time_since_seen > 300:
                        status = "⚠️  OFFLINE"
                    else:
                        status = "✅ ONLINE"
                else:
                    status = "❓ UNKNOWN"
                
                print(f"  {status} {device_id} ({device_type}) - Requesting location...")
                
                result = await self.fcm_service.request_location(device_id, priority="normal")
                
                if result.get('status') == 'sent':
                    print(f"    ✓ Request sent: {result.get('message_id', 'N/A')}")
                else:
                    print(f"    ✗ Failed: {result.get('error', 'Unknown error')}")
                
            except Exception as e:
                print(f"    ✗ Error for {device_id}: {e}")
        
        print(f"[LocationTracker] Location requests sent at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")


class DeviceManager:
    """Manages FCM device registration and messaging."""
    
    def __init__(self):
        self.devices = {}  # device_id -> device_info
        self.token_to_device = {}  # fcm_token -> device_id
        self.pending_requests = {}  # request_id -> request_info
        
        # Initialize Firebase Admin SDK
        try:
            cred = credentials.Certificate(FIREBASE_SERVICE_ACCOUNT)
            firebase_admin.initialize_app(cred)
            print("[FCM] Firebase Admin SDK initialized successfully")
        except Exception as e:
            print(f"[FCM] Firebase initialization failed: {e}")
            raise
    
    def register_device(self, device_data: dict) -> dict:
        """Register a device with its FCM token."""
        device_id = device_data.get('device_id')
        fcm_token = device_data.get('fcm_token')
        device_type = device_data.get('device_type', 'unknown')
        
        if not device_id or not fcm_token:
            raise ValueError("device_id and fcm_token are required")
        
        # Store device info
        self.devices[device_id] = {
            'fcm_token': fcm_token,
            'device_type': device_type,
            'app_version': device_data.get('app_version', 'unknown'),
            'os_version': device_data.get('os_version', 'unknown'),
            'registered_at': device_data.get('timestamp', datetime.now().isoformat()),
            'last_seen': datetime.now().isoformat(),
            'is_active': True
        }
        
        # Reverse lookup
        self.token_to_device[fcm_token] = device_id
        
        print(f"[FCM] Device registered: {device_id} ({device_type})")
        
        return {
            'status': 'success',
            'device_id': device_id,
            'registered_at': self.devices[device_id]['registered_at']
        }
    
    def get_device(self, device_id: str) -> Optional[dict]:
        """Get device info by ID."""
        return self.devices.get(device_id)
    
    def get_token(self, device_id: str) -> Optional[str]:
        """Get FCM token for a device."""
        device = self.devices.get(device_id)
        return device['fcm_token'] if device else None
    
    def get_devices_by_type(self, device_type: str) -> list:
        """Get all devices of a specific type."""
        return [
            device_id for device_id, info in self.devices.items()
            if info['device_type'] == device_type and info['is_active']
        ]
    
    def get_all_tokens(self) -> list:
        """Get all active FCM tokens."""
        return [
            info['fcm_token'] for info in self.devices.values()
            if info['is_active']
        ]
    
    def get_tokens_by_type(self, device_type: str) -> list:
        """Get FCM tokens for devices of a specific type."""
        return [
            info['fcm_token'] for device_id, info in self.devices.items()
            if info['device_type'] == device_type and info['is_active']
        ]
    
    def update_last_seen(self, device_id: str) -> None:
        """Update device's last seen timestamp."""
        if device_id in self.devices:
            self.devices[device_id]['last_seen'] = datetime.now().isoformat()
    
    def deactivate_device(self, device_id: str) -> bool:
        """Mark device as inactive."""
        if device_id in self.devices:
            self.devices[device_id]['is_active'] = False
            return True
        return False
    
    def get_stats(self) -> dict:
        """Get device statistics."""
        total = len(self.devices)
        active = sum(1 for d in self.devices.values() if d['is_active'])
        phones = sum(1 for d in self.devices.values() if d['device_type'] == 'phone' and d['is_active'])
        watches = sum(1 for d in self.devices.values() if d['device_type'] == 'watch' and d['is_active'])
        
        return {
            'total_devices': total,
            'active_devices': active,
            'phones': phones,
            'watches': watches
        }


class FCMNotificationService:
    """Service for sending FCM push notifications."""
    
    def __init__(self, device_manager: DeviceManager):
        self.device_manager = device_manager
    
    async def send_to_device(self, device_id: str, title: str, body: str, data: dict = None) -> dict:
        """Send notification to a specific device."""
        token = self.device_manager.get_token(device_id)
        if not token:
            raise ValueError(f"Device not found: {device_id}")
        
        message = messaging.Message(
            notification=messaging.Notification(
                title=title,
                body=body,
            ),
            data=data or {},
            token=token,
        )
        
        try:
            response = messaging.send(message)
            print(f"[FCM] Sent to {device_id}: {response}")
            return {'status': 'sent', 'message_id': response, 'device_id': device_id}
        except Exception as e:
            print(f"[FCM] Failed to send to {device_id}: {e}")
            return {'status': 'failed', 'error': str(e), 'device_id': device_id}
    
    async def send_to_multiple(self, device_ids: list, title: str, body: str, data: dict = None) -> dict:
        """Send notification to multiple devices."""
        tokens = [self.device_manager.get_token(did) for did in device_ids]
        tokens = [t for t in tokens if t]  # Filter None values
        
        if not tokens:
            return {'status': 'failed', 'error': 'No valid tokens found'}
        
        message = messaging.MulticastMessage(
            notification=messaging.Notification(
                title=title,
                body=body,
            ),
            data=data or {},
            tokens=tokens,
        )
        
        try:
            response = messaging.send_multicast(message)
            print(f"[FCM] Multicast: {response.success_count} sent, {response.failure_count} failed")
            return {
                'status': 'sent',
                'success_count': response.success_count,
                'failure_count': response.failure_count
            }
        except Exception as e:
            print(f"[FCM] Multicast failed: {e}")
            return {'status': 'failed', 'error': str(e)}
    
    async def send_to_type(self, device_type: str, title: str, body: str, data: dict = None) -> dict:
        """Send notification to all devices of a type (phone/watch)."""
        device_ids = self.device_manager.get_devices_by_type(device_type)
        return await self.send_to_multiple(device_ids, title, body, data)
    
    async def broadcast_to_all(self, title: str, body: str, data: dict = None) -> dict:
        """Send notification to all registered devices."""
        device_ids = list(self.device_manager.devices.keys())
        return await self.send_to_multiple(device_ids, title, body, data)
    
    async def request_location(self, device_id: str, priority: str = "high") -> dict:
        """Request GPS location from a device with high-priority background execution."""
        request_id = f"loc_{device_id}_{int(time.time())}"
        return await self.send_silent_data(
            device_id,
            {
                "action": "get_location",
                "priority": priority,
                "accuracy": "high",
                "request_id": request_id
            }
        )
    
    async def request_location_from_all(self, priority: str = "normal") -> dict:
        """Request location from all devices with high-priority background execution."""
        device_ids = list(self.device_manager.devices.keys())
        request_id = f"loc_all_{int(time.time())}"
        
        tokens = [self.device_manager.get_token(did) for did in device_ids]
        tokens = [t for t in tokens if t]  # Filter None values
        
        if not tokens:
            return {'status': 'failed', 'error': 'No valid tokens found'}
        
        message = messaging.MulticastMessage(
            data={
                "action": "get_location",
                "priority": priority,
                "request_id": request_id
            },
            tokens=tokens,
            android=messaging.AndroidConfig(
                priority='high',  # HIGH PRIORITY: Executes immediately in background
            )
        )
        
        try:
            response = messaging.send_multicast(message)
            print(f"[FCM] Multicast: {response.success_count} sent, {response.failure_count} failed")
            return {
                'status': 'sent',
                'success_count': response.success_count,
                'failure_count': response.failure_count
            }
        except Exception as e:
            print(f"[FCM] Multicast failed: {e}")
            return {'status': 'failed', 'error': str(e)}
    
    async def execute_command(self, device_id: str, command: str) -> dict:
        """Execute a command on a device with high-priority background execution."""
        request_id = f"cmd_{device_id}_{int(time.time())}"
        return await self.send_silent_data(
            device_id,
            {
                "action": "execute_command",
                "command": command,
                "request_id": request_id
            }
        )
    
    async def send_silent_data(self, device_id: str, data: dict) -> dict:
        """
        Send high-priority data-only message that executes immediately in background.
        
        CRITICAL: No notification field = executes without user interaction.
        Android priority 'high' = wakes device and executes immediately.
        """
        token = self.device_manager.get_token(device_id)
        if not token:
            raise ValueError(f"Device not found: {device_id}")
        
        message = messaging.Message(
            data=data,
            token=token,
            android=messaging.AndroidConfig(
                priority='high',  # HIGH PRIORITY: Wakes device immediately
                # NO notification field = silent execution in background
            )
        )
        
        try:
            response = messaging.send(message)
            print(f"[FCM] Silent data sent to {device_id}: {response}")
            return {'status': 'sent', 'message_id': response, 'device_id': device_id}
        except Exception as e:
            print(f"[FCM] Failed to send to {device_id}: {e}")
            return {'status': 'failed', 'error': str(e), 'device_id': device_id}


# ============================================================================
# LLM FACTORY
# ============================================================================

def create_llm_client(provider: str = "cerebras") -> BaseLLMClient:
    """Factory function to create LLM client based on provider."""
    if provider == "gemini":
        return GeminiLLMClient(api_key=GEMINI_API_KEY, model=GEMINI_MODEL)
    elif provider == "cerebras":
        return CerebrasLLMClient(api_key=CEREBRAS_API_KEY, model=CEREBRAS_MODEL)
    else:
        raise ValueError(f"Unknown LLM provider: {provider}")


# ============================================================================
# SESSION STATISTICS
# ============================================================================

@dataclass
class SessionStatistics:
    """Comprehensive statistics tracking for a voice session."""
    
    session_id: str = ""
    client_type: str = ""
    llm_provider: str = ""
    llm_model: str = ""
    
    # Timestamps (all in Unix epoch seconds)
    timestamps: dict = field(default_factory=lambda: {
        'session_start': None,
        'stt_connected': None,
        'tts_connected': None,
        'first_audio_received': None,
        'speech_start': None,
        'first_transcript': None,
        'speech_end': None,
        'final_transcript': None,
        'llm_request_sent': None,
        'first_llm_token': None,
        'first_llm_chunk': None,
        'first_tts_chunk_sent': None,
        'first_audio_response': None,
        'llm_complete': None,
        'last_audio_response': None,
        'session_end': None,
    })
    
    # Metrics
    metrics: dict = field(default_factory=lambda: {
        'input_word_count': 0,
        'output_word_count': 0,
        'output_char_count': 0,
        'transcript_updates': 0,
        'final_segments': 0,
        'llm_chunks_generated': 0,
        'llm_tokens': [],  # Token timestamps for inter-token latency
        'llm_chunk_times': [],  # Chunk timestamps
        'audio_chunks_sent': 0,
        'audio_bytes_sent': 0,
        'audio_chunks_received': 0,
        'audio_bytes_received': 0,
    })
    
    # Content
    transcript: str = ""
    response: str = ""
    
    def compute_latencies(self) -> dict:
        """Compute all latency metrics in milliseconds."""
        ts = self.timestamps
        latencies = {}
        
        def ms_diff(end_key: str, start_key: str) -> Optional[int]:
            if ts.get(end_key) and ts.get(start_key):
                return int((ts[end_key] - ts[start_key]) * 1000)
            return None
        
        latencies['stt_connect_ms'] = ms_diff('stt_connected', 'session_start')
        latencies['tts_connect_ms'] = ms_diff('tts_connected', 'session_start')
        latencies['time_to_first_transcript_ms'] = ms_diff('first_transcript', 'speech_start')
        latencies['stt_processing_ms'] = ms_diff('final_transcript', 'speech_end')
        latencies['time_to_first_llm_token_ms'] = ms_diff('first_llm_token', 'llm_request_sent')
        latencies['time_to_first_llm_chunk_ms'] = ms_diff('first_llm_chunk', 'llm_request_sent')
        latencies['tts_latency_ms'] = ms_diff('first_audio_response', 'first_tts_chunk_sent')
        latencies['voice_response_latency_ms'] = ms_diff('first_audio_response', 'speech_end')
        latencies['total_response_latency_ms'] = ms_diff('last_audio_response', 'speech_end')
        latencies['total_session_ms'] = ms_diff('session_end', 'session_start')
        
        # Compute speech duration
        speech_duration_ms = ms_diff('speech_end', 'speech_start')
        if speech_duration_ms:
            latencies['speech_duration_ms'] = speech_duration_ms
        
        # Remove None values
        return {k: v for k, v in latencies.items() if v is not None}
    
    def compute_metrics(self) -> dict:
        """Compute all metrics for the session."""
        m = self.metrics.copy()
        
        # Word counts
        if self.transcript:
            m['input_word_count'] = len(self.transcript.split())
        if self.response:
            m['output_word_count'] = len(self.response.split())
            m['output_char_count'] = len(self.response)
        
        # Speaking rate
        speech_duration = None
        if self.timestamps.get('speech_end') and self.timestamps.get('speech_start'):
            speech_duration = self.timestamps['speech_end'] - self.timestamps['speech_start']
            if speech_duration > 0 and m['input_word_count'] > 0:
                m['speaking_rate_wpm'] = int(m['input_word_count'] / speech_duration * 60)
        
        # Inter-chunk latency
        chunk_times = m.get('llm_chunk_times', [])
        if len(chunk_times) > 1:
            intervals = [chunk_times[i] - chunk_times[i-1] for i in range(1, len(chunk_times))]
            m['avg_inter_chunk_ms'] = int(sum(intervals) / len(intervals) * 1000)
        
        # Clean up internal tracking fields
        m.pop('llm_tokens', None)
        m.pop('llm_chunk_times', None)
        
        # Add provider info
        m['llm_provider'] = self.llm_provider
        m['llm_model'] = self.llm_model
        
        return m
    
    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            'session_id': self.session_id,
            'client_type': self.client_type,
            'timestamp': datetime.now().isoformat(),
            'timestamps': {k: v for k, v in self.timestamps.items() if v is not None},
            'latencies': self.compute_latencies(),
            'metrics': self.compute_metrics(),
            'transcript': self.transcript,
            'response': self.response,
        }
    
    def to_message(self) -> dict:
        """Create a stats message for client."""
        return create_message(
            MessageType.STATS,
            session_id=self.session_id,
            timestamps=self.timestamps,
            latencies=self.compute_latencies(),
            metrics=self.compute_metrics()
        )
    
    def save_to_file(self, filepath: str = STATS_FILE) -> None:
        """Append stats to JSON Lines file."""
        with open(filepath, 'a') as f:
            f.write(json.dumps(self.to_dict()) + '\n')
    
    def display(self) -> None:
        """Display formatted statistics to console."""
        latencies = self.compute_latencies()
        metrics = self.compute_metrics()
        
        print("\n" + "=" * 60)
        print(f"📊 SESSION STATISTICS [{self.session_id}]")
        print("=" * 60)
        
        # STT Timing
        print("\n⏱️  STT TIMING:")
        if latencies.get('speech_duration_ms'):
            print(f"   Speech Duration:           {latencies['speech_duration_ms']/1000:.2f}s")
        if latencies.get('time_to_first_transcript_ms'):
            print(f"   Time to First Transcript:  {latencies['time_to_first_transcript_ms']}ms")
        if latencies.get('stt_processing_ms'):
            print(f"   STT Processing Latency:    {latencies['stt_processing_ms']}ms")
        if latencies.get('stt_connect_ms'):
            print(f"   WebSocket Connect Time:    {latencies['stt_connect_ms']}ms")
        
        # LLM Timing
        print("\n🤖 LLM TIMING:")
        if latencies.get('time_to_first_llm_token_ms'):
            print(f"   Time to First Token:       {latencies['time_to_first_llm_token_ms']}ms")
        if latencies.get('time_to_first_llm_chunk_ms'):
            print(f"   Time to First Chunk:       {latencies['time_to_first_llm_chunk_ms']}ms")
        if metrics.get('avg_inter_chunk_ms'):
            print(f"   Avg Inter-Chunk Delay:     {metrics['avg_inter_chunk_ms']}ms")
        
        # TTS Timing
        print("\n🔊 TTS TIMING:")
        if latencies.get('tts_latency_ms'):
            print(f"   Time to First Audio:       {latencies['tts_latency_ms']}ms")
        if latencies.get('tts_connect_ms'):
            print(f"   TTS Connect Time:          {latencies['tts_connect_ms']}ms")
        print(f"   Audio Chunks Received:     {metrics.get('audio_chunks_received', 0)}")
        audio_kb = metrics.get('audio_bytes_received', 0) / 1024
        print(f"   Total Audio Output:        {audio_kb:.1f} KB")
        
        # End-to-End
        print("\n🔄 END-TO-END PIPELINE:")
        if latencies.get('voice_response_latency_ms'):
            print(f"   Voice Response Latency:    {latencies['voice_response_latency_ms']}ms ⭐")
            # Rating
            vrl = latencies['voice_response_latency_ms']
            if vrl < 1500:
                print(f"   Rating:                    ⭐⭐⭐ Excellent (<1.5s)")
            elif vrl < 2500:
                print(f"   Rating:                    ⭐⭐ Good (<2.5s)")
            else:
                print(f"   Rating:                    ⭐ Needs improvement (>2.5s)")
        if latencies.get('total_session_ms'):
            print(f"   Total Session Duration:    {latencies['total_session_ms']/1000:.2f}s")
        
        # Transcript Stats
        print("\n📝 TRANSCRIPT:")
        print(f"   Input Words:               {metrics.get('input_word_count', 0)}")
        if metrics.get('speaking_rate_wpm'):
            print(f"   Speaking Rate:             {metrics['speaking_rate_wpm']} words/min")
        print(f"   Transcript Updates:        {metrics.get('transcript_updates', 0)}")
        print(f"   Final Segments:            {metrics.get('final_segments', 0)}")
        
        # Response Stats
        print("\n🗣️  LLM RESPONSE:")
        print(f"   Output Words:              {metrics.get('output_word_count', 0)}")
        print(f"   Output Characters:         {metrics.get('output_char_count', 0)}")
        print(f"   TTS Chunks Generated:      {metrics.get('llm_chunks_generated', 0)}")
        
        # Network
        print("\n🌐 NETWORK:")
        print(f"   Audio Chunks Sent:         {metrics.get('audio_chunks_sent', 0)}")
        audio_sent_kb = metrics.get('audio_bytes_sent', 0) / 1024
        print(f"   Audio Data Sent:           {audio_sent_kb:.1f} KB")
        
        print("\n" + "=" * 60)


# ============================================================================
# CARTESIA STT CLIENT (Server-side)
# ============================================================================

class CartesiaSTTClient:
    """Cartesia STT WebSocket client for server-side processing."""
    
    def __init__(self, on_transcript=None, on_final=None, stats: SessionStatistics = None):
        self.ws = None
        self.is_connected = False
        self.full_transcript = ""
        self.current_partial = ""
        self.session_done = False
        self.on_transcript = on_transcript  # callback for transcript updates
        self.on_final = on_final  # callback when final transcript ready
        self.stats = stats  # Reference to session stats
        
        # VAD state
        self.speech_started = False
        self.last_speech_time = None
        self.vad_triggered = False
        self.silence_threshold = VAD_THRESHOLD
        
    def build_url(self) -> str:
        query_string = urlencode(CARTESIA_PARAMS)
        return f"{CARTESIA_STT_URL}?{query_string}"
    
    async def connect(self) -> None:
        url = self.build_url()
        try:
            self.ws = await websockets.connect(url, ping_interval=20, ping_timeout=10, close_timeout=5)
            self.is_connected = True
            if self.stats:
                self.stats.timestamps['stt_connected'] = time.time()
        except Exception as e:
            print(f"[STT] Connection failed: {e}")
            raise
    
    def calculate_rms(self, audio_data: bytes) -> float:
        """Calculate RMS energy of audio chunk."""
        count = len(audio_data) // 2
        if count == 0:
            return 0
        shorts = struct.unpack(f"{count}h", audio_data)
        sum_squares = sum(s ** 2 for s in shorts)
        return (sum_squares / count) ** 0.5
    
    def check_voice_activity(self, audio_data: bytes) -> bool:
        """Check if audio chunk contains speech."""
        rms = self.calculate_rms(audio_data)
        is_speech = rms > self.silence_threshold
        current_time = time.time()
        
        if is_speech:
            self.last_speech_time = current_time
            if not self.speech_started:
                self.speech_started = True
                if self.stats:
                    self.stats.timestamps['speech_start'] = current_time
            return True
        else:
            if self.speech_started and self.last_speech_time:
                silence_duration = current_time - self.last_speech_time
                if silence_duration >= SILENCE_DURATION and not self.vad_triggered:
                    self.vad_triggered = True
                    if self.stats:
                        self.stats.timestamps['speech_end'] = self.last_speech_time
                    return False
            return False
    
    async def send_audio(self, audio_data: bytes) -> None:
        """Send audio chunk to Cartesia."""
        if self.ws and self.is_connected and not self.session_done:
            try:
                await self.ws.send(audio_data)
                if self.stats:
                    self.stats.metrics['audio_chunks_sent'] += 1
                    self.stats.metrics['audio_bytes_sent'] += len(audio_data)
            except websockets.exceptions.ConnectionClosed:
                self.is_connected = False
    
    async def send_finalize(self) -> None:
        """Send finalize message to flush remaining audio."""
        if self.ws and self.is_connected:
            try:
                await self.ws.send("finalize")
            except websockets.exceptions.ConnectionClosed:
                pass
    
    async def send_done(self) -> None:
        """Send done message to close session."""
        if self.ws and self.is_connected:
            try:
                await self.ws.send("done")
            except websockets.exceptions.ConnectionClosed:
                pass
    
    async def receive_messages(self) -> None:
        """Receive and process messages from Cartesia."""
        try:
            async for message in self.ws:
                if isinstance(message, str):
                    await self.process_message(message)
                    if self.session_done:
                        break
        except websockets.exceptions.ConnectionClosed:
            self.is_connected = False
    
    async def process_message(self, message: str) -> None:
        """Process a JSON message from Cartesia."""
        try:
            data = json.loads(message)
            msg_type = data.get("type", "")
            
            if msg_type == "transcript":
                text = data.get("text", "")
                is_final = data.get("is_final", False)
                
                if self.stats:
                    self.stats.metrics['transcript_updates'] += 1
                    if not self.stats.timestamps.get('first_transcript'):
                        self.stats.timestamps['first_transcript'] = time.time()
                
                if is_final:
                    if self.full_transcript and not self.full_transcript.endswith(" "):
                        self.full_transcript += " "
                    self.full_transcript += text
                    self.current_partial = ""
                    if self.stats:
                        self.stats.metrics['final_segments'] += 1
                else:
                    self.current_partial = text
                
                # Callback with transcript update
                if self.on_transcript:
                    await self.on_transcript(
                        self.full_transcript + self.current_partial,
                        is_final
                    )
            
            elif msg_type == "done":
                self.session_done = True
                if self.stats:
                    self.stats.timestamps['final_transcript'] = time.time()
                    self.stats.transcript = self.full_transcript.strip()
                if self.on_final:
                    await self.on_final(self.full_transcript.strip())
            
            elif msg_type == "error":
                print(f"[STT] Error: {data.get('message', 'Unknown')}")
                self.session_done = True
                
        except json.JSONDecodeError:
            pass
    
    async def close(self) -> None:
        """Close the WebSocket connection."""
        if self.ws:
            await self.ws.close()
            self.is_connected = False


# ============================================================================
# CARTESIA TTS CLIENT (Server-side)
# ============================================================================

class CartesiaTTSClient:
    """Cartesia TTS WebSocket client for server-side processing."""
    
    def __init__(self, on_audio=None, stats: SessionStatistics = None):
        self.ws = None
        self.is_connected = False
        self.context_id = "jarvis-response"
        self.on_audio = on_audio  # callback for audio chunks
        self.total_audio_bytes = 0
        self.first_audio_time = None
        self.stats = stats  # Reference to session stats
        
    def build_url(self) -> str:
        params = {
            "api_key": CARTESIA_API_KEY,
            "cartesia_version": "2024-11-13",
        }
        return f"{CARTESIA_TTS_URL}?{urlencode(params)}"
    
    async def connect(self) -> None:
        url = self.build_url()
        try:
            self.ws = await websockets.connect(url, ping_interval=20, ping_timeout=10, close_timeout=5)
            self.is_connected = True
            if self.stats:
                self.stats.timestamps['tts_connected'] = time.time()
        except Exception as e:
            print(f"[TTS] Connection failed: {e}")
            raise
    
    async def send_text_chunk(self, text: str, is_last: bool = False) -> None:
        """Send a text chunk for TTS conversion."""
        if not self.ws or not self.is_connected:
            return
        
        if self.stats and not self.stats.timestamps.get('first_tts_chunk_sent'):
            self.stats.timestamps['first_tts_chunk_sent'] = time.time()
        
        request = {
            "model_id": TTS_MODEL,
            "transcript": text,
            "voice": {"mode": "id", "id": TTS_VOICE_ID},
            "output_format": {
                "container": "raw",
                "encoding": "pcm_s16le",
                "sample_rate": OUTPUT_SAMPLE_RATE
            },
            "language": "en",
            "context_id": self.context_id,
            "continue": not is_last,
            "generation_config": {
                "speed": TTS_SPEED,
                "volume": TTS_VOLUME,
                "emotion": TTS_EMOTION
            }
        }
        
        try:
            await self.ws.send(json.dumps(request))
        except websockets.exceptions.ConnectionClosed:
            self.is_connected = False
    
    async def receive_audio(self) -> None:
        """Receive audio chunks and forward via callback."""
        if not self.ws:
            return
        
        try:
            async for message in self.ws:
                if isinstance(message, str):
                    data = json.loads(message)
                    msg_type = data.get("type", "")
                    
                    if msg_type == "chunk":
                        audio_b64 = data.get("data", "")
                        if audio_b64:
                            audio_bytes = base64.b64decode(audio_b64)
                            
                            if not self.first_audio_time:
                                self.first_audio_time = time.time()
                                if self.stats:
                                    self.stats.timestamps['first_audio_response'] = self.first_audio_time
                            
                            self.total_audio_bytes += len(audio_bytes)
                            
                            if self.stats:
                                self.stats.metrics['audio_chunks_received'] += 1
                                self.stats.metrics['audio_bytes_received'] += len(audio_bytes)
                                self.stats.timestamps['last_audio_response'] = time.time()
                            
                            if self.on_audio:
                                await self.on_audio(audio_bytes, data.get("done", False))
                        
                        if data.get("done", False):
                            break
                    
                    elif msg_type == "done":
                        break
                    
                    elif msg_type == "error":
                        print(f"[TTS] Error: {data.get('message', 'Unknown')}")
                        break
                        
        except websockets.exceptions.ConnectionClosed:
            self.is_connected = False
    
    async def close(self) -> None:
        """Close connection."""
        if self.ws:
            await self.ws.close()
        self.is_connected = False


# ============================================================================
# VOICE SESSION HANDLER
# ============================================================================

class VoiceSession:
    """Handles a single voice session with a client."""
    
    def __init__(self, client_ws, llm_provider: str = "cerebras"):
        self.client_ws = client_ws  # Can be WebSocketServerProtocol or aiohttp WebSocketResponse
        self.session_id = str(uuid.uuid4())[:8]
        self.llm_provider = llm_provider
        
        self.stt_client: Optional[CartesiaSTTClient] = None
        self.llm_client: Optional[BaseLLMClient] = None
        self.tts_client: Optional[CartesiaTTSClient] = None
        
        self.audio_queue: asyncio.Queue = asyncio.Queue()
        self.is_active = False
        self.full_transcript = ""
        self.full_response = ""
        
        # Events
        self.stt_done_event = asyncio.Event()
        self.processing_complete = asyncio.Event()
        
        # Initialize statistics
        self.stats = SessionStatistics(
            session_id=self.session_id,
            llm_provider=llm_provider,
            llm_model=CEREBRAS_MODEL if llm_provider == "cerebras" else GEMINI_MODEL
        )
    
    async def send_to_client(self, message: dict) -> None:
        """Send JSON message to client."""
        try:
            # Support both websockets and aiohttp WebSocket
            if hasattr(self.client_ws, 'send_json'):
                await self.client_ws.send_json(message)
            else:
                await self.client_ws.send(json.dumps(message))
        except (websockets.exceptions.ConnectionClosed, ConnectionResetError, RuntimeError):
            self.is_active = False
    
    async def send_audio_to_client(self, audio_bytes: bytes, is_final: bool = False) -> None:
        """Send audio chunk to client."""
        # Send as raw binary for efficiency
        try:
            # Support both websockets and aiohttp WebSocket
            if hasattr(self.client_ws, 'send_bytes'):
                await self.client_ws.send_bytes(audio_bytes)
            else:
                await self.client_ws.send(audio_bytes)
        except (websockets.exceptions.ConnectionClosed, ConnectionResetError, RuntimeError):
            self.is_active = False
    
    async def on_transcript_update(self, text: str, is_final: bool) -> None:
        """Callback for transcript updates - forward to client."""
        msg = create_message(MessageType.TRANSCRIPT, text=text, is_final=is_final)
        await self.send_to_client(msg)
    
    async def on_final_transcript(self, text: str) -> None:
        """Callback when final transcript is ready."""
        self.full_transcript = text
        self.stt_done_event.set()
    
    async def on_tts_audio(self, audio_bytes: bytes, is_done: bool) -> None:
        """Callback for TTS audio chunks - forward to client."""
        await self.send_audio_to_client(audio_bytes, is_final=is_done)
    
    async def start(self, client_type: str = "unknown") -> None:
        """Initialize and start the session."""
        self.is_active = True
        self.stats.timestamps['session_start'] = time.time()
        self.stats.client_type = client_type
        
        # Create STT client with callbacks and stats
        self.stt_client = CartesiaSTTClient(
            on_transcript=self.on_transcript_update,
            on_final=self.on_final_transcript,
            stats=self.stats
        )
        
        # Create LLM client
        self.llm_client = create_llm_client(self.llm_provider)
        
        # Create TTS client with callback and stats
        self.tts_client = CartesiaTTSClient(
            on_audio=self.on_tts_audio,
            stats=self.stats
        )
        
        # Connect to services
        await self.stt_client.connect()
        
        # Pre-connect TTS for lower latency
        tts_connect_task = asyncio.create_task(self.tts_client.connect())
        
        # Notify client
        await self.send_to_client(create_message(
            MessageType.SESSION_STARTED,
            session_id=self.session_id
        ))
        await self.send_to_client(create_message(
            MessageType.SESSION_READY,
            message="Ready for audio"
        ))
        
        print(f"[{self.session_id}] Session started, waiting for audio...")
        
        # Start STT receiver task
        stt_receiver_task = asyncio.create_task(self.stt_client.receive_messages())
        
        # Process audio from queue
        await self.process_audio_stream()
        
        # Wait for STT to complete
        await self.stt_done_event.wait()
        
        # Ensure TTS is connected
        await tts_connect_task
        
        # Process through LLM + TTS if we have a transcript
        if self.full_transcript:
            print(f"[{self.session_id}] Transcript: {self.full_transcript}")
            
            await self.send_to_client(create_message(
                MessageType.PROCESSING,
                stage="llm",
                message="Generating response..."
            ))
            
            await self.llm_to_tts_pipeline()
            
            # Finalize stats
            self.stats.timestamps['session_end'] = time.time()
            self.stats.response = self.full_response.strip()
            
            # Display and save stats
            self.stats.display()
            self.stats.save_to_file()
            
            # Send done message with stats summary
            done_msg = create_message(
                MessageType.DONE,
                transcript=self.full_transcript,
                response=self.full_response.strip(),
                stats=self.stats.to_dict()
            )
            await self.send_to_client(done_msg)
            
            # Send detailed stats message
            await self.send_to_client(self.stats.to_message())
        else:
            self.stats.timestamps['session_end'] = time.time()
            await self.send_to_client(create_message(
                MessageType.DONE,
                transcript="",
                response=""
            ))
        
        # Cleanup
        await self.cleanup()
    
    async def process_audio_stream(self) -> None:
        """Process incoming audio chunks."""
        while self.is_active and not self.stt_done_event.is_set():
            try:
                # Wait for audio with timeout
                audio_chunk = await asyncio.wait_for(
                    self.audio_queue.get(),
                    timeout=0.1
                )
                
                if audio_chunk is None:
                    # End of audio stream
                    break
                
                # Track first audio received
                if not self.stats.timestamps.get('first_audio_received'):
                    self.stats.timestamps['first_audio_received'] = time.time()
                
                # Check VAD
                self.stt_client.check_voice_activity(audio_chunk)
                
                # Send to STT
                await self.stt_client.send_audio(audio_chunk)
                
                # If VAD triggered end of speech
                if self.stt_client.vad_triggered and not self.stt_client.session_done:
                    print(f"[{self.session_id}] Silence detected, finalizing...")
                    await self.stt_client.send_finalize()
                    await self.stt_client.send_done()
                    break
                    
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                print(f"[{self.session_id}] Audio processing error: {e}")
                break
    
    async def receive_audio(self, audio_data: bytes) -> None:
        """Queue incoming audio chunk for processing."""
        if self.is_active:
            await self.audio_queue.put(audio_data)
    
    async def end_of_speech(self) -> None:
        """Client signals end of speech."""
        if self.stt_client and not self.stt_client.session_done:
            print(f"[{self.session_id}] Client signaled end of speech")
            # Set speech_end if not already set by VAD
            if not self.stats.timestamps.get('speech_end'):
                self.stats.timestamps['speech_end'] = time.time()
            await self.stt_client.send_finalize()
            await self.stt_client.send_done()
    
    async def llm_to_tts_pipeline(self) -> None:
        """Run LLM and TTS in parallel - same as main.py."""
        tts_queue: asyncio.Queue = asyncio.Queue()
        self.stats.timestamps['llm_request_sent'] = time.time()
        
        async def llm_producer():
            chunk_num = 0
            first_token_recorded = False
            first_chunk_recorded = False
            
            try:
                async for chunk in self.llm_client.generate_streaming_response(self.full_transcript):
                    current_time = time.time()
                    
                    # Track first token (approximation - first yield)
                    if not first_token_recorded:
                        self.stats.timestamps['first_llm_token'] = current_time
                        first_token_recorded = True
                    
                    chunk_num += 1
                    self.full_response += chunk + " "
                    
                    # Track chunk times
                    self.stats.metrics['llm_chunk_times'].append(current_time)
                    self.stats.metrics['llm_chunks_generated'] = chunk_num
                    
                    if not first_chunk_recorded:
                        self.stats.timestamps['first_llm_chunk'] = current_time
                        first_chunk_recorded = True
                    
                    # Send text chunk to client for display
                    await self.send_to_client(create_message(
                        MessageType.RESPONSE_TEXT,
                        text=chunk,
                        chunk_index=chunk_num
                    ))
                    
                    await tts_queue.put((chunk, False))
                
                await tts_queue.put((None, True))
                self.stats.timestamps['llm_complete'] = time.time()
                
            except Exception as e:
                print(f"[{self.session_id}] LLM error: {e}")
                await tts_queue.put((None, True))
        
        async def tts_consumer():
            while True:
                text, is_last = await tts_queue.get()
                if text is None:
                    break
                await self.tts_client.send_text_chunk(text, is_last=is_last)
        
        async def tts_receiver():
            await self.tts_client.receive_audio()
        
        await asyncio.gather(llm_producer(), tts_consumer(), tts_receiver())
    
    async def cancel(self) -> None:
        """Cancel the current session."""
        self.is_active = False
        await self.audio_queue.put(None)
        await self.cleanup()
    
    async def cleanup(self) -> None:
        """Cleanup session resources."""
        self.is_active = False
        
        if self.stt_client:
            await self.stt_client.close()
        if self.tts_client:
            await self.tts_client.close()
        
        print(f"[{self.session_id}] Session ended")


# ============================================================================
# HTTP REST API (for device registration & FCM)
# ============================================================================

class HTTPAPIServer:
    """Unified HTTP/WebSocket server for device management, FCM, and voice assistant."""
    
    def __init__(self, device_manager: DeviceManager, fcm_service: FCMNotificationService, 
                 llm_provider: str, auth_token: str, location_tracker: LocationTracker):
        self.device_manager = device_manager
        self.fcm_service = fcm_service
        self.llm_provider = llm_provider
        self.auth_token = auth_token
        self.location_tracker = location_tracker
        self.app = web.Application()
        self.active_sessions: dict[str, VoiceSession] = {}
        self.setup_routes()
    
    def setup_routes(self):
        """Setup HTTP and WebSocket routes."""
        # WebSocket endpoint for voice assistant
        self.app.router.add_get('/ws', self.websocket_handler)
        self.app.router.add_get('/ws/locations', self.location_websocket_handler)
        
        # Web dashboard
        self.app.router.add_get('/', self.serve_dashboard)
        self.app.router.add_get('/dashboard', self.serve_dashboard)
        
        # Location API endpoints
        self.app.router.add_get('/api/locations', self.get_locations)
        self.app.router.add_get('/api/locations/{device_id}', self.get_device_locations)
        
        # HTTP API endpoints for FCM and device management
        self.app.router.add_post('/api/register-device', self.register_device)
        self.app.router.add_post('/api/request-location/{device_id}', self.request_location)
        self.app.router.add_post('/api/request-location-all', self.request_location_all)
        self.app.router.add_post('/api/execute-command/{device_id}', self.execute_command)
        self.app.router.add_post('/api/send-to-type/{device_type}', self.send_to_type)
        self.app.router.add_post('/api/send-notification/{device_id}', self.send_notification)
        self.app.router.add_post('/api/broadcast', self.broadcast)
        self.app.router.add_post('/api/location-update', self.location_update)
        self.app.router.add_post('/api/device-response', self.device_response)
        self.app.router.add_get('/api/devices', self.list_devices)
        self.app.router.add_get('/api/devices/{device_id}', self.get_device_info)
        self.app.router.add_get('/api/stats', self.get_stats)
        self.app.router.add_get('/api/debug/devices', self.debug_devices)  # Debug endpoint
        self.app.router.add_delete('/api/devices/{device_id}', self.deactivate_device)
    
    def validate_token(self, request: web.Request) -> bool:
        """Validate authentication token from query parameter."""
        token = request.query.get('token')
        return token == self.auth_token
    
    async def serve_dashboard(self, request: web.Request) -> web.Response:
        """Serve the location tracking dashboard."""
        html_path = Path(__file__).parent / 'dashboard.html'
        if html_path.exists():
            return web.FileResponse(html_path)
        else:
            # Return inline HTML if file doesn't exist
            return web.Response(text="Dashboard not found. Run setup to create dashboard.html", content_type='text/html')
    
    async def get_locations(self, request: web.Request) -> web.Response:
        """Get location data with optional filtering."""
        try:
            limit = int(request.query.get('limit', 20))
            device_id = request.query.get('device_id')
            
            # Read locations from file
            locations = []
            if Path(self.location_tracker.filepath).exists():
                with open(self.location_tracker.filepath, 'r') as f:
                    for line in f:
                        if line.strip():
                            loc = json.loads(line)
                            if device_id is None or loc['device_id'] == device_id:
                                locations.append(loc)
            
            # Sort by timestamp descending and limit
            locations.sort(key=lambda x: x['timestamp'], reverse=True)
            locations = locations[:limit]
            
            return web.json_response({
                'locations': locations,
                'count': len(locations),
                'limit': limit
            })
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)
    
    async def get_device_locations(self, request: web.Request) -> web.Response:
        """Get locations for a specific device."""
        device_id = request.match_info['device_id']
        limit = int(request.query.get('limit', 20))
        
        try:
            locations = []
            if Path(self.location_tracker.filepath).exists():
                with open(self.location_tracker.filepath, 'r') as f:
                    for line in f:
                        if line.strip():
                            loc = json.loads(line)
                            if loc['device_id'] == device_id:
                                locations.append(loc)
            
            # Sort by timestamp descending and limit
            locations.sort(key=lambda x: x['timestamp'], reverse=True)
            locations = locations[:limit]
            
            return web.json_response({
                'device_id': device_id,
                'locations': locations,
                'count': len(locations)
            })
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)
    
    async def location_websocket_handler(self, request: web.Request) -> web.WebSocketResponse:
        """WebSocket handler for real-time location updates."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        
        # Store websocket for broadcasting
        if not hasattr(self, 'location_ws_clients'):
            self.location_ws_clients = set()
        self.location_ws_clients.add(ws)
        
        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    # Handle ping/pong
                    data = json.loads(msg.data)
                    if data.get('type') == 'ping':
                        await ws.send_json({'type': 'pong'})
        finally:
            self.location_ws_clients.discard(ws)
        
        return ws
    
    async def websocket_handler(self, request: web.Request) -> web.WebSocketResponse:
        """Handle WebSocket connections for voice assistant."""
        
        # Prepare WebSocket FIRST (complete handshake)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        
        # THEN validate authentication
        if not self.validate_token(request):
            # Send error message via WebSocket and close gracefully
            await ws.send_json({
                "type": "error",
                "message": "Unauthorized: Invalid or missing token"
            })
            await ws.close(code=4401, message=b'Unauthorized')
            print(f"[Server] Rejected connection: invalid token from {request.remote}")
            return ws
        
        client_id = f"{request.remote}:{request.transport.get_extra_info('peername')[1] if request.transport else 'unknown'}"
        print(f"[Server] New authenticated WebSocket connection from {client_id}")
        
        session: Optional[VoiceSession] = None
        
        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.BINARY:
                    # Handle binary audio data
                    if session and session.is_active:
                        await session.receive_audio(msg.data)
                
                elif msg.type == web.WSMsgType.TEXT:
                    # Handle JSON messages
                    try:
                        data = json.loads(msg.data)
                        msg_type = data.get("type", "")
                        
                        if msg_type == MessageType.SESSION_START.value:
                            # Start new session
                            if session:
                                await session.cancel()
                            
                            # Create session with aiohttp WebSocket wrapper
                            session = VoiceSession(ws, self.llm_provider)
                            self.active_sessions[session.session_id] = session
                            
                            # Get client type from message
                            client_type = data.get("client_type", "unknown")
                            
                            # Run session in background
                            asyncio.create_task(session.start(client_type=client_type))
                        
                        elif msg_type == MessageType.AUDIO.value:
                            # Base64 encoded audio
                            if session and session.is_active:
                                audio_bytes = base64.b64decode(data.get("data", ""))
                                await session.receive_audio(audio_bytes)
                        
                        elif msg_type == MessageType.END_OF_SPEECH.value:
                            if session:
                                await session.end_of_speech()
                        
                        elif msg_type == MessageType.CANCEL.value:
                            if session:
                                await session.cancel()
                                session = None
                        
                        elif msg_type == MessageType.PING.value:
                            await ws.send_json({"type": MessageType.PONG.value})
                    
                    except json.JSONDecodeError:
                        # Might be raw binary that looks like string
                        if session and session.is_active:
                            await session.receive_audio(msg.data.encode() if isinstance(msg.data, str) else msg.data)
                
                elif msg.type == web.WSMsgType.ERROR:
                    print(f'[Server] WebSocket error: {ws.exception()}')
                    break
        
        except Exception as e:
            print(f"[Server] Error handling WebSocket client {client_id}: {e}")
            import traceback
            traceback.print_exc()
        finally:
            if session:
                await session.cancel()
                if session.session_id in self.active_sessions:
                    del self.active_sessions[session.session_id]
            print(f"[Server] WebSocket client disconnected: {client_id}")
        
        return ws
    
    async def register_device(self, request: web.Request) -> web.Response:
        """Register a new device."""
        try:
            data = await request.json()
            print(f"\n[Registration] Received device registration:")
            print(f"  Device ID: {data.get('device_id')}")
            print(f"  Device Type: {data.get('device_type')}")
            print(f"  FCM Token: {data.get('fcm_token', 'N/A')[:20]}...")
            print(f"  Full data: {json.dumps(data, indent=2)}\n")
            
            result = self.device_manager.register_device(data)
            
            print(f"[Registration] ✓ Device registered successfully: {data.get('device_id')}")
            print(f"[Registration] Total devices now: {len(self.device_manager.devices)}\n")
            
            return web.json_response(result)
        except ValueError as e:
            print(f"[Registration] ✗ Registration failed: {e}\n")
            return web.json_response({'error': str(e)}, status=400)
        except Exception as e:
            print(f"[Registration] ✗ Internal error: {e}")
            import traceback
            traceback.print_exc()
            return web.json_response({'error': f'Internal error: {str(e)}'}, status=500)
    
    async def request_location(self, request: web.Request) -> web.Response:
        """Request location from a specific device."""
        device_id = request.match_info['device_id']
        priority = request.query.get('priority', 'high')
        
        try:
            result = await self.fcm_service.request_location(device_id, priority)
            return web.json_response(result)
        except ValueError as e:
            return web.json_response({'error': str(e)}, status=404)
        except Exception as e:
            return web.json_response({'error': f'Internal error: {str(e)}'}, status=500)
    
    async def request_location_all(self, request: web.Request) -> web.Response:
        """Request location from all devices."""
        priority = request.query.get('priority', 'normal')
        result = await self.fcm_service.request_location_from_all(priority)
        return web.json_response(result)
    
    async def execute_command(self, request: web.Request) -> web.Response:
        """Execute a command on a specific device."""
        device_id = request.match_info['device_id']
        
        try:
            data = await request.json()
            command = data.get('command')
            
            if not command:
                return web.json_response({'error': 'command is required'}, status=400)
            
            result = await self.fcm_service.execute_command(device_id, command)
            return web.json_response(result)
        except ValueError as e:
            return web.json_response({'error': str(e)}, status=404)
        except Exception as e:
            return web.json_response({'error': f'Internal error: {str(e)}'}, status=500)
    
    async def send_to_type(self, request: web.Request) -> web.Response:
        """Send notification to all devices of a type."""
        device_type = request.match_info['device_type']
        
        try:
            data = await request.json()
            title = data.get('title', 'JARVIS')
            body = data.get('body', 'Message from server')
            notification_data = data.get('data', {})
            
            result = await self.fcm_service.send_to_type(device_type, title, body, notification_data)
            return web.json_response(result)
        except Exception as e:
            return web.json_response({'error': f'Internal error: {str(e)}'}, status=500)
    
    async def send_notification(self, request: web.Request) -> web.Response:
        """Send notification to a specific device."""
        device_id = request.match_info['device_id']
        
        try:
            data = await request.json()
            title = data.get('title', 'JARVIS')
            body = data.get('body', 'Message from server')
            notification_data = data.get('data', {})
            
            result = await self.fcm_service.send_to_device(device_id, title, body, notification_data)
            return web.json_response(result)
        except ValueError as e:
            return web.json_response({'error': str(e)}, status=404)
        except Exception as e:
            return web.json_response({'error': f'Internal error: {str(e)}'}, status=500)
    
    async def broadcast(self, request: web.Request) -> web.Response:
        """Broadcast notification to all devices."""
        try:
            data = await request.json()
            title = data.get('title', 'JARVIS Broadcast')
            body = data.get('body', 'Message from server')
            notification_data = data.get('data', {})
            
            result = await self.fcm_service.broadcast_to_all(title, body, notification_data)
            return web.json_response(result)
        except Exception as e:
            return web.json_response({'error': f'Internal error: {str(e)}'}, status=500)
    
    async def location_update(self, request: web.Request) -> web.Response:
        """Receive location update from device."""
        try:
            data = await request.json()
            device_id = data.get('device_id')
            latitude = data.get('latitude')
            longitude = data.get('longitude')
            
            print(f"[Location] {device_id}: {latitude}, {longitude}")
            print(f"[Location] Full data: {json.dumps(data, indent=2)}")
            
            # Update last seen
            if self.device_manager:
                self.device_manager.update_last_seen(device_id)
            
            # Broadcast to WebSocket clients
            if hasattr(self, 'location_ws_clients'):
                for ws in list(self.location_ws_clients):
                    try:
                        await ws.send_json({
                            'type': 'location_update',
                            'data': data
                        })
                    except:
                        self.location_ws_clients.discard(ws)
            
            return web.json_response({'status': 'received', 'device_id': device_id})
        except Exception as e:
            return web.json_response({'error': f'Internal error: {str(e)}'}, status=500)
    
    async def device_response(self, request: web.Request) -> web.Response:
        """Receive response from device after action execution."""
        try:
            data = await request.json()
            
            device_id = data.get('device_id')
            device_type = data.get('device_type', 'unknown')
            action = data.get('action')
            status = data.get('status')
            request_id = data.get('request_id')
            timestamp = data.get('timestamp', time.time() * 1000) / 1000  # Convert ms to seconds
            
            print(f"\n[DeviceResponse] {device_id} ({device_type}) - {action}: {status}")
            
            # Update last seen (device is online)
            self.device_manager.update_last_seen(device_id)
            
            # Handle based on action type
            if action == 'get_location':
                if status == 'success':
                    location_data = data.get('data', {})
                    lat = location_data.get('latitude')
                    lon = location_data.get('longitude')
                    accuracy = location_data.get('accuracy', 0)
                    altitude = location_data.get('altitude')
                    speed = location_data.get('speed')
                    bearing = location_data.get('bearing')
                    provider = location_data.get('provider', 'unknown')
                    
                    print(f"  📍 Location: {lat:.6f}, {lon:.6f}")
                    print(f"  📊 Accuracy: ±{accuracy:.1f}m | Provider: {provider}")
                    if altitude is not None:
                        print(f"  🏔️  Altitude: {altitude:.1f}m")
                    if speed is not None and speed > 0:
                        print(f"  🚗 Speed: {speed:.1f} m/s ({speed * 3.6:.1f} km/h)")
                    if bearing is not None:
                        print(f"  🧭 Bearing: {bearing:.1f}°")
                    
                    # Create location record
                    location = LocationRecord(
                        device_id=device_id,
                        device_type=device_type,
                        latitude=lat,
                        longitude=lon,
                        accuracy=accuracy,
                        altitude=altitude,
                        speed=speed,
                        bearing=bearing,
                        provider=provider,
                        timestamp=timestamp
                    )
                    
                    # Save to tracker
                    self.location_tracker.update_location(location)
                    print(f"  ✓ Location saved to {self.location_tracker.filepath}")
                    
                    # Broadcast to WebSocket clients
                    if hasattr(self, 'location_ws_clients'):
                        for ws in list(self.location_ws_clients):
                            try:
                                await ws.send_json({
                                    'type': 'location_update',
                                    'data': location.to_dict()
                                })
                            except:
                                self.location_ws_clients.discard(ws)
                    
                else:
                    error = data.get('error', 'Unknown error')
                    exception = data.get('exception')
                    print(f"  ✗ Location failed: {error}")
                    if exception:
                        print(f"    Exception: {exception}")
            
            elif action == 'execute_command':
                if status == 'success':
                    command_data = data.get('data', {})
                    command = command_data.get('command')
                    result = command_data.get('result')
                    
                    print(f"  ✓ Command '{command}' executed: {result}")
                else:
                    error = data.get('error', 'Unknown error')
                    print(f"  ✗ Command failed: {error}")
            
            # Store request response for tracking
            if request_id:
                self.device_manager.pending_requests[request_id] = {
                    'response': data,
                    'received_at': time.time()
                }
            
            # Log full response for debugging
            print(f"  📋 Full response: {json.dumps(data, indent=2)}\n")
            
            return web.json_response({'status': 'acknowledged'})
            
        except Exception as e:
            print(f"[DeviceResponse] Error: {e}")
            import traceback
            traceback.print_exc()
            return web.json_response({'error': f'Internal error: {str(e)}'}, status=500)
    
    async def list_devices(self, request: web.Request) -> web.Response:
        """List all registered devices."""
        devices = {
            device_id: {
                'device_type': info['device_type'],
                'app_version': info['app_version'],
                'os_version': info['os_version'],
                'registered_at': info['registered_at'],
                'last_seen': info['last_seen'],
                'is_active': info['is_active']
            }
            for device_id, info in self.device_manager.devices.items()
        }
        return web.json_response({'devices': devices})
    
    async def get_device_info(self, request: web.Request) -> web.Response:
        """Get info for a specific device."""
        device_id = request.match_info['device_id']
        device = self.device_manager.get_device(device_id)
        
        if not device:
            return web.json_response({'error': 'Device not found'}, status=404)
        
        # Don't expose FCM token in response
        safe_device = {k: v for k, v in device.items() if k != 'fcm_token'}
        return web.json_response({'device_id': device_id, **safe_device})
    
    async def get_stats(self, request: web.Request) -> web.Response:
        """Get device statistics."""
        stats = self.device_manager.get_stats()
        return web.json_response(stats)
    
    async def deactivate_device(self, request: web.Request) -> web.Response:
        """Deactivate a device."""
        device_id = request.match_info['device_id']
        success = self.device_manager.deactivate_device(device_id)
        
        if not success:
            return web.json_response({'error': 'Device not found'}, status=404)
        
        return web.json_response({'status': 'deactivated', 'device_id': device_id})
    
    async def debug_devices(self, request: web.Request) -> web.Response:
        """Debug endpoint to check device registration status."""
        if not self.device_manager:
            return web.json_response({
                'error': 'FCM disabled',
                'device_manager': None
            })
        
        devices_info = {
            'total_devices': len(self.device_manager.devices),
            'devices': {}
        }
        
        for device_id, info in self.device_manager.devices.items():
            last_seen_str = info.get('last_seen')
            if last_seen_str:
                last_seen = datetime.fromisoformat(last_seen_str)
                time_since = (datetime.now() - last_seen).total_seconds()
                status = "ONLINE" if time_since < 300 else "OFFLINE"
            else:
                status = "UNKNOWN"
            
            devices_info['devices'][device_id] = {
                'device_type': info['device_type'],
                'is_active': info['is_active'],
                'status': status,
                'last_seen': last_seen_str,
                'registered_at': info['registered_at'],
                'app_version': info.get('app_version', 'unknown'),
                'has_fcm_token': bool(info.get('fcm_token'))
            }
        
        return web.json_response(devices_info)


# ============================================================================
# UNIFIED SERVER
# ============================================================================

class JarvisServer:
    """Unified HTTP/WebSocket server for Jarvis voice assistant and device management."""
    
    def __init__(self, host: str = "0.0.0.0", port: int = 8000,
                 llm_provider: str = "cerebras", auth_token: str = None,
                 enable_fcm: bool = True, location_interval: int = 30):
        self.host = host
        self.port = port
        self.llm_provider = llm_provider
        self.auth_token = auth_token or AUTH_TOKEN
        self.enable_fcm = enable_fcm
        self.location_interval = location_interval
        
        # FCM components
        self.device_manager: Optional[DeviceManager] = None
        self.fcm_service: Optional[FCMNotificationService] = None
        self.location_tracker: Optional[LocationTracker] = None
        self.periodic_tracker: Optional[PeriodicLocationTracker] = None
        self.http_api: Optional[HTTPAPIServer] = None
        
        if enable_fcm:
            try:
                self.device_manager = DeviceManager()
                self.fcm_service = FCMNotificationService(self.device_manager)
                self.location_tracker = LocationTracker()
                self.periodic_tracker = PeriodicLocationTracker(
                    self.fcm_service,
                    self.device_manager,
                    self.location_tracker,
                    interval_minutes=location_interval
                )
                self.http_api = HTTPAPIServer(
                    self.device_manager, 
                    self.fcm_service,
                    self.llm_provider,
                    self.auth_token,
                    self.location_tracker
                )
                print("[Server] FCM services initialized")
            except Exception as e:
                print(f"[Server] FCM initialization failed: {e}")
                print("[Server] Continuing without FCM support")
                self.enable_fcm = False
        else:
            # Create HTTP API without FCM
            self.location_tracker = LocationTracker()
            self.http_api = HTTPAPIServer(
                None, 
                None,
                self.llm_provider,
                self.auth_token,
                self.location_tracker
            )
    
    async def start(self) -> None:
        """Start the unified HTTP/WebSocket server."""
        print("\n" + "=" * 60)
        print("  JARVIS VOICE ASSISTANT SERVER")
        print("=" * 60)
        print(f"\n  Host: {self.host}")
        print(f"  Port: {self.port}")
        print(f"  LLM Provider: {self.llm_provider}")
        print(f"  Auth Token: {self.auth_token[:4]}...{self.auth_token[-4:]}")
        print(f"\n  WebSocket URL: ws://{self.host}:{self.port}/ws?token=<your_token>")
        print(f"  HTTP API URL: http://{self.host}:{self.port}/api")
        print(f"\n  Stats File: {STATS_FILE}")
        if self.enable_fcm:
            print(f"\n  FCM: Enabled")
            print(f"    - Device Registration: POST /api/register-device")
            print(f"    - Request Location: POST /api/request-location/{{device_id}}")
            print(f"    - Execute Command: POST /api/execute-command/{{device_id}}")
            print(f"    - List Devices: GET /api/devices")
            print(f"\n  Location Tracking: Every {self.location_interval} minutes")
            print(f"    - Location File: device_locations.jsonl")
        print("\n" + "=" * 60)
        print("\nWaiting for connections...\n")
        
        # Start unified server
        runner = web.AppRunner(self.http_api.app)
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        await site.start()
        
        print(f"[Server] Server started on {self.host}:{self.port}")
        print(f"[Server] WebSocket endpoint: /ws")
        print(f"[Server] HTTP API endpoints: /api/*")
        
        # Start periodic location tracking
        if self.enable_fcm and self.periodic_tracker:
            await self.periodic_tracker.start()
        
        print()
        
        # Run forever
        try:
            await asyncio.Future()
        finally:
            # Cleanup on shutdown
            if self.periodic_tracker:
                await self.periodic_tracker.stop()


# ============================================================================
# AUTO-RELOAD FUNCTIONALITY
# ============================================================================

class ServerReloader:
    """Monitors files for changes and restarts the server."""
    
    def __init__(self, watch_dirs=None, watch_extensions=None):
        self.watch_dirs = watch_dirs or [Path.cwd()]
        self.watch_extensions = watch_extensions or {'.py'}
        self.file_mtimes = {}
        self.process = None
        self.should_exit = False
        
        # Scan initial file modification times
        self._scan_files()
    
    def _scan_files(self):
        """Scan all monitored files and store their modification times."""
        for watch_dir in self.watch_dirs:
            for ext in self.watch_extensions:
                for filepath in Path(watch_dir).rglob(f'*{ext}'):
                    try:
                        self.file_mtimes[filepath] = filepath.stat().st_mtime
                    except OSError:
                        pass
    
    def check_for_changes(self) -> bool:
        """Check if any monitored files have changed."""
        changed_files = []
        
        # Check existing files for modifications
        for filepath, old_mtime in list(self.file_mtimes.items()):
            try:
                new_mtime = filepath.stat().st_mtime
                if new_mtime != old_mtime:
                    changed_files.append(filepath)
                    self.file_mtimes[filepath] = new_mtime
            except OSError:
                # File was deleted
                changed_files.append(filepath)
                del self.file_mtimes[filepath]
        
        # Check for new files
        for watch_dir in self.watch_dirs:
            for ext in self.watch_extensions:
                for filepath in Path(watch_dir).rglob(f'*{ext}'):
                    if filepath not in self.file_mtimes:
                        try:
                            self.file_mtimes[filepath] = filepath.stat().st_mtime
                            changed_files.append(filepath)
                        except OSError:
                            pass
        
        if changed_files:
            print(f"\n[Reloader] Detected changes in:")
            for f in changed_files:
                print(f"  - {f.name}")
            return True
        return False
    
    def start_server(self, args):
        """Start the server as a subprocess."""
        cmd = [sys.executable] + sys.argv
        # Remove --reload flag to avoid infinite recursion
        cmd = [arg for arg in cmd if arg != '--reload']
        
        print(f"\n[Reloader] Starting server...")
        self.process = subprocess.Popen(cmd)
        return self.process
    
    def stop_server(self):
        """Stop the server subprocess."""
        if self.process:
            print(f"\n[Reloader] Stopping server...")
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            self.process = None
    
    def run(self, args):
        """Main reload loop."""
        print("\n" + "=" * 60)
        print("  🔄 AUTO-RELOAD MODE ENABLED")
        print("=" * 60)
        print(f"\n  Monitoring: {', '.join(str(d) for d in self.watch_dirs)}")
        print(f"  Extensions: {', '.join(self.watch_extensions)}")
        print(f"  Files tracked: {len(self.file_mtimes)}")
        print("\n  Press Ctrl+C to stop")
        print("=" * 60)
        
        def signal_handler(signum, frame):
            self.should_exit = True
            self.stop_server()
            sys.exit(0)
        
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        
        # Start initial server
        self.start_server(args)
        
        try:
            while not self.should_exit:
                time.sleep(1)  # Check every second
                
                if self.check_for_changes():
                    self.stop_server()
                    print("[Reloader] Restarting server...\n")
                    time.sleep(0.5)  # Brief pause before restart
                    self.start_server(args)
        
        except KeyboardInterrupt:
            pass
        finally:
            self.stop_server()


# ============================================================================
# MAIN
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="Jarvis Voice Assistant Server")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000, help="Server port (default: 8000)")
    parser.add_argument("--llm", default="cerebras", choices=["gemini", "cerebras"],
                        help="LLM provider (default: cerebras)")
    parser.add_argument("--token", default=None, 
                        help="Authentication token (default: from JARVIS_AUTH_TOKEN env or 'Denemeler123.')")
    parser.add_argument("--no-fcm", action="store_true",
                        help="Disable FCM functionality")
    parser.add_argument("--location-interval", type=int, default=1,
                        help="Location tracking interval in minutes (default: 30)")
    parser.add_argument("--reload", action="store_true",
                        help="Enable auto-reload on file changes")
    
    args = parser.parse_args()
    
    # If reload mode, start the reloader instead
    if args.reload:
        reloader = ServerReloader(
            watch_dirs=[Path.cwd()],
            watch_extensions={'.py', '.json'}
        )
        reloader.run(args)
        return
    
    server = JarvisServer(
        host=args.host, 
        port=args.port,
        llm_provider=args.llm,
        auth_token=args.token,
        enable_fcm=not args.no_fcm,
        location_interval=args.location_interval
    )
    
    try:
        asyncio.run(server.start())
    except KeyboardInterrupt:
        print("\n\n[Server] Shutting down...")


if __name__ == "__main__":
    main()
