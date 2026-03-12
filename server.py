"""Jarvis Voice Assistant - WebSocket Server."""

import asyncio
import json
import os
import time
import base64
import argparse
from typing import Optional
from pathlib import Path

from aiohttp import web
import websockets

# Import protocol definitions
from protocol import MessageType, INPUT_SAMPLE_RATE, create_message

# Import LLM providers
from llm import GeminiLLMClient, CerebrasLLMClient

# Import our modular components
from voice_assistant import VoiceSession
from location_tracking import LocationTracker, LocationRecord, PeriodicLocationTracker
from fcm_service import DeviceManager, FCMNotificationService

# ============================================================================
# CONFIGURATION
# ============================================================================

# Authentication
AUTH_TOKEN = os.environ.get("JARVIS_AUTH_TOKEN", "Denemeler123.")

# API Keys
CARTESIA_API_KEY = os.environ.get("CARTESIA_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
CEREBRAS_API_KEY = os.environ.get("CEREBRAS_API_KEY")

# Firebase
FIREBASE_SERVICE_ACCOUNT = os.environ.get(
    "FIREBASE_SERVICE_ACCOUNT",
    "jarvis-app-43084-firebase-adminsdk-fbsvc-c836619551.json"
)

# LLM configuration
GEMINI_MODEL = "models/gemini-2.5-flash-lite"
CEREBRAS_MODEL = "gpt-oss-120b"

# Transform endpoint rate limiting
TRANSFORM_RATE_LIMIT = 30  # max requests per minute per IP

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
# FIREBASE CREDENTIAL ENCRYPTION HELPERS
# ============================================================================

def _derive_fernet_key(token: str) -> bytes:
    """Derive a 256-bit Fernet key from the auth token using SHA-256."""
    import hashlib, base64
    return base64.urlsafe_b64encode(hashlib.sha256(token.encode()).digest())


def _load_firebase_creds(service_account_path: str, auth_token: str) -> dict:
    """Load Firebase service account credentials.

    Priority:
      1. Plain JSON file  → load directly (local dev)
      2. <path>.enc file  → decrypt with auth_token (cloned from repo)

    Encrypted files are created with ``python encrypt_creds.py``.
    """
    from cryptography.fernet import Fernet, InvalidToken

    plain = Path(service_account_path)
    enc = Path(service_account_path + ".enc")

    if plain.exists():
        import json
        with open(plain) as f:
            return json.load(f)

    if enc.exists():
        key = _derive_fernet_key(auth_token)
        try:
            decrypted = Fernet(key).decrypt(enc.read_bytes())
        except InvalidToken:
            raise ValueError(
                f"Failed to decrypt '{enc}' — JARVIS_AUTH_TOKEN is wrong or the file is corrupt."
            )
        import json
        return json.loads(decrypted)

    raise FileNotFoundError(
        f"Firebase credentials not found.\n"
        f"  Expected plain file : {plain}\n"
        f"  Or encrypted file   : {enc}\n"
        f"  To encrypt your credentials run: python encrypt_creds.py"
    )


# ============================================================================
# LLM FACTORY
# ============================================================================

def create_llm_client(provider: str = "cerebras"):
    """Factory function to create LLM client based on provider."""
    if provider == "gemini":
        return GeminiLLMClient(api_key=GEMINI_API_KEY, model=GEMINI_MODEL)
    elif provider == "cerebras":
        return CerebrasLLMClient(api_key=CEREBRAS_API_KEY, model=CEREBRAS_MODEL)
    else:
        raise ValueError(f"Unknown LLM provider: {provider}")


# ============================================================================
# TEXT TRANSFORMER (for /api/transform endpoint)
# ============================================================================

# Configurable system prompt for the transform endpoint
TRANSFORM_SYSTEM_PROMPT = (
    "You are a text corrector assistant. "
    "Process the user's text exactly as instructed and return only the result, "
    "with no additional commentary, explanation, or formatting."
    "i want you to correct the text i have written, but keep it in the vibe and sound as i written and keep the acronyms as they are like, u, jz, etc. "
    "keep the language that i written the text in that might be english, german anything keep that language in your response"
    "so just correct the small grammer mistakes like in german the artikel mistakes like der, des oder zur, zu, zum etc."
    "and generally but like i said keep the style that i ve written"
    "but if i write somethin inside a {} curly braces and it really doenst fit the context, they are my query spesific instructions and use those instruction as your base like i might occasionally write something informal but want u to write it more professionally and formal do that and not as i described before, but only if i instructed u about it "
)


class TextTransformer:
    """Stateless LLM text transformer for the /api/transform endpoint.

    Completely separate from the voice assistant pipeline.
    No memory, no streaming, no TTS - just text in, text out.
    """

    def __init__(self, provider: str = "cerebras", system_prompt: str = TRANSFORM_SYSTEM_PROMPT):
        self.provider = provider
        self.system_prompt = system_prompt

        if provider == "cerebras":
            from cerebras.cloud.sdk import Cerebras as _Cerebras
            self._client = _Cerebras(api_key=CEREBRAS_API_KEY)
            self._model = CEREBRAS_MODEL
        elif provider == "gemini":
            from google import genai as _genai
            self._client = _genai.Client(api_key=GEMINI_API_KEY)
            self._model = GEMINI_MODEL
        else:
            raise ValueError(f"Unknown LLM provider: {provider}")

    async def transform(self, text: str) -> str:
        """Send text to the LLM and return the processed result."""
        loop = asyncio.get_event_loop()

        if self.provider == "cerebras":
            def _call():
                response = self._client.chat.completions.create(
                    messages=[
                        {"role": "system", "content": self.system_prompt},
                        {"role": "user", "content": text},
                    ],
                    model=self._model,
                    stream=False,
                    max_tokens=1024,
                )
                return response.choices[0].message.content

            return await loop.run_in_executor(None, _call)

        elif self.provider == "gemini":
            def _call():
                response = self._client.models.generate_content(
                    model=self._model,
                    contents=text,
                    config={
                        "system_instruction": self.system_prompt,
                        "max_output_tokens": 1024,
                    },
                )
                return response.text

            return await loop.run_in_executor(None, _call)


# ============================================================================
# RATE LIMITER & AUTH MIDDLEWARE
# ============================================================================

class RateLimiter:
    """Simple in-memory per-key rate limiter with a sliding window."""

    def __init__(self, max_requests: int, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._buckets: dict[str, list[float]] = {}

    def is_allowed(self, key: str) -> bool:
        now = time.time()
        cutoff = now - self.window_seconds
        bucket = [t for t in self._buckets.get(key, []) if t > cutoff]
        if len(bucket) >= self.max_requests:
            self._buckets[key] = bucket
            return False
        bucket.append(now)
        self._buckets[key] = bucket
        return True


def _make_auth_middleware(auth_token: str):
    """Enforce token auth on every route and add security headers to all HTTP responses.

    Clients must supply the token via:
      - Query param:  ?token=<token>
      - HTTP header:  Authorization: Bearer <token>
    """

    @web.middleware
    async def middleware(request: web.Request, handler):
        # Health check is always allowed without auth
        if request.path == '/health':
            return await handler(request)

        auth_header = request.headers.get('Authorization', '')
        bearer = auth_header[7:].strip() if auth_header.startswith('Bearer ') else ''
        token = request.query.get('token') or bearer

        if token != auth_token:
            if request.path.rstrip('/') in ('', '/dashboard'):
                return web.Response(
                    text=(
                        '<h1>401 Unauthorized</h1>'
                        '<p>Append <code>?token=YOUR_TOKEN</code> to the URL.</p>'
                    ),
                    content_type='text/html',
                    status=401,
                )
            return web.json_response(
                {'error': 'Unauthorized: invalid or missing token'}, status=401
            )

        response = await handler(request)

        # Security headers — skip WebSocket upgrades (already sent)
        if not isinstance(response, web.WebSocketResponse):
            response.headers.setdefault('X-Content-Type-Options', 'nosniff')
            response.headers.setdefault('X-Frame-Options', 'DENY')
            response.headers.setdefault('X-XSS-Protection', '1; mode=block')
            response.headers.setdefault('Referrer-Policy', 'no-referrer')

        return response

    return middleware


# ============================================================================
# HTTP REST API (for device registration & FCM)
# ============================================================================

class HTTPAPIServer:
    """Unified HTTP/WebSocket server for device management, FCM, and voice assistant."""
    
    def __init__(self, device_manager: DeviceManager, fcm_service: FCMNotificationService, 
                 llm_provider: str, auth_token: str, location_tracker: LocationTracker,
                 cartesia_api_key: str, cartesia_params: dict,
                 text_transformer: 'TextTransformer'):
        self.device_manager = device_manager
        self.fcm_service = fcm_service
        self.llm_provider = llm_provider
        self.auth_token = auth_token
        self.location_tracker = location_tracker
        self.cartesia_api_key = cartesia_api_key
        self.cartesia_params = cartesia_params
        self.text_transformer = text_transformer
        self.app = web.Application(middlewares=[_make_auth_middleware(self.auth_token)])
        self.active_sessions: dict[str, VoiceSession] = {}
        self._transform_limiter = RateLimiter(TRANSFORM_RATE_LIMIT)
        self.setup_routes()
    
    def setup_routes(self):
        """Setup HTTP and WebSocket routes."""
        # Health check – no auth required (for Koyeb / load balancer probes)
        self.app.router.add_get('/health', self.health_check)

        # WebSocket endpoints
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
        self.app.router.add_get('/api/debug/devices', self.debug_devices)
        self.app.router.add_delete('/api/devices/{device_id}', self.deactivate_device)
        
        # Text transform endpoint (keyboard app)
        self.app.router.add_post('/api/transform', self.transform_text)
    
    def validate_token(self, request: web.Request) -> bool:
        """Validate authentication token from query parameter."""
        token = request.query.get('token')
        return token == self.auth_token

    async def health_check(self, request: web.Request) -> web.Response:
        """Unauthenticated health check endpoint for Koyeb / load balancers."""
        return web.json_response({'status': 'ok'})

    async def serve_dashboard(self, request: web.Request) -> web.Response:
        """Serve the location tracking dashboard."""
        html_path = Path(__file__).parent / 'dashboard.html'
        if html_path.exists():
            return web.FileResponse(html_path)
        else:
            return web.Response(text="Dashboard not found. Run setup to create dashboard.html", content_type='text/html')
    
    async def get_locations(self, request: web.Request) -> web.Response:
        """Get location data with optional filtering."""
        try:
            limit = int(request.query.get('limit', 20))
            device_id = request.query.get('device_id')
            
            locations = []
            if Path(self.location_tracker.filepath).exists():
                with open(self.location_tracker.filepath, 'r') as f:
                    for line in f:
                        if line.strip():
                            loc = json.loads(line)
                            if device_id is None or loc['device_id'] == device_id:
                                locations.append(loc)
            
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
        
        if not hasattr(self, 'location_ws_clients'):
            self.location_ws_clients = set()
        self.location_ws_clients.add(ws)
        
        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    if data.get('type') == 'ping':
                        await ws.send_json({'type': 'pong'})
        finally:
            self.location_ws_clients.discard(ws)
        
        return ws
    
    async def websocket_handler(self, request: web.Request) -> web.WebSocketResponse:
        """Handle WebSocket connections for voice assistant."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        
        if not self.validate_token(request):
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
                    if session and session.is_active:
                        await session.receive_audio(msg.data)
                
                elif msg.type == web.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        msg_type = data.get("type", "")
                        
                        if msg_type == MessageType.SESSION_START.value:
                            if session:
                                await session.cancel()
                            
                            # Create LLM client
                            llm_client = create_llm_client(self.llm_provider)
                            llm_model = CEREBRAS_MODEL if self.llm_provider == "cerebras" else GEMINI_MODEL
                            
                            session = VoiceSession(
                                ws, llm_client, self.cartesia_api_key,
                                self.cartesia_params, self.llm_provider, llm_model
                            )
                            self.active_sessions[session.session_id] = session
                            
                            client_type = data.get("client_type", "unknown")
                            asyncio.create_task(session.start(client_type=client_type))
                        
                        elif msg_type == MessageType.AUDIO.value:
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
            
            if self.device_manager:
                self.device_manager.update_last_seen(device_id)
            
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
            timestamp = data.get('timestamp', time.time() * 1000) / 1000
            
            print(f"\n[DeviceResponse] {device_id} ({device_type}) - {action}: {status}")
            
            self.device_manager.update_last_seen(device_id)
            
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
                    
                    self.location_tracker.update_location(location)
                    print(f"  ✓ Location saved to {self.location_tracker.filepath}")
                    
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
                    print(f"  ✗ Location failed: {error}")
            
            elif action == 'execute_command':
                if status == 'success':
                    command_data = data.get('data', {})
                    command = command_data.get('command')
                    result = command_data.get('result')
                    print(f"  ✓ Command '{command}' executed: {result}")
                else:
                    error = data.get('error', 'Unknown error')
                    print(f"  ✗ Command failed: {error}")
            
            if request_id:
                self.device_manager.pending_requests[request_id] = {
                    'response': data,
                    'received_at': time.time()
                }
            
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
    
    async def transform_text(self, request: web.Request) -> web.Response:
        """Transform text via LLM - stateless, no memory, no streaming."""
        try:
            data = await request.json()
            text = data.get('text', '')

            if not text:
                return web.json_response({'error': 'text is required'}, status=400)

            if not text.strip():
                return web.json_response({'result': text})

            if not self._transform_limiter.is_allowed(request.remote or 'unknown'):
                print(f"[Transform]   Rate limit exceeded for {request.remote}")
                return web.json_response({'error': 'Rate limit exceeded. Try again later.'}, status=429)

            print(f"\n[Transform] ← {request.remote}")
            print(f"[Transform]   Input : {text!r}")

            result = await self.text_transformer.transform(text)

            print(f"[Transform]   Output: {result!r}")

            return web.json_response({'result': result})
        except Exception as e:
            print(f"[Transform]   Error : {e}")
            return web.json_response({'error': str(e)}, status=500)
    
    async def debug_devices(self, request: web.Request) -> web.Response:
        """Debug endpoint to check device registration status."""
        from datetime import datetime
        
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
        
        # Text transformer is always created, independent of FCM
        self.text_transformer = TextTransformer(provider=self.llm_provider)
        print(f"[Server] TextTransformer initialized ({self.llm_provider})")

        if enable_fcm:
            try:
                firebase_creds = _load_firebase_creds(FIREBASE_SERVICE_ACCOUNT, self.auth_token)
                self.device_manager = DeviceManager(firebase_creds)
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
                    self.location_tracker,
                    CARTESIA_API_KEY,
                    CARTESIA_PARAMS,
                    self.text_transformer
                )
                print("[Server] FCM services initialized")
            except Exception as e:
                print(f"[Server] FCM initialization failed: {e}")
                print("[Server] Continuing without FCM support")
                self.enable_fcm = False
        
        if not self.enable_fcm or not self.http_api:
            self.location_tracker = LocationTracker()
            self.http_api = HTTPAPIServer(
                None, 
                None,
                self.llm_provider,
                self.auth_token,
                self.location_tracker,
                CARTESIA_API_KEY,
                CARTESIA_PARAMS,
                self.text_transformer
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
        
        runner = web.AppRunner(self.http_api.app)
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        await site.start()
        
        print(f"[Server] Server started on {self.host}:{self.port}")
        print(f"[Server] WebSocket endpoint: /ws")
        print(f"[Server] HTTP API endpoints: /api/*\n")
        
        if self.enable_fcm and self.periodic_tracker:
            await self.periodic_tracker.start()
        
        try:
            await asyncio.Future()
        finally:
            if self.periodic_tracker:
                await self.periodic_tracker.stop()


# ============================================================================
# MAIN
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="Jarvis Voice Assistant Server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("PORT", 8000)),
                        help="Server port (default: PORT env var or 8000)")
    parser.add_argument("--llm", default=os.environ.get("LLM_PROVIDER", "cerebras"),
                        choices=["gemini", "cerebras"])
    parser.add_argument("--no-fcm", action="store_true")
    parser.add_argument("--location-interval", type=int, default=30)

    args = parser.parse_args()

    server = JarvisServer(
        host=args.host,
        port=args.port,
        llm_provider=args.llm,
        enable_fcm=not args.no_fcm,
        location_interval=args.location_interval
    )

    try:
        asyncio.run(server.start())
    except KeyboardInterrupt:
        print("\n[Server] Shutting down...")


if __name__ == "__main__":
    main()
