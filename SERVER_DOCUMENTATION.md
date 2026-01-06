# JARVIS Voice Assistant Server - Complete Documentation

**Version:** 1.0  
**Last Updated:** January 6, 2026

---

## Table of Contents

1. [Overview](#overview)
2. [Server Architecture](#server-architecture)
3. [Getting Started](#getting-started)
4. [WebSocket Voice Assistant API](#websocket-voice-assistant-api)
5. [HTTP REST API (FCM & Device Management)](#http-rest-api-fcm--device-management)
6. [Device Registration Flow](#device-registration-flow)
7. [Push Notification Actions](#push-notification-actions)
8. [Client Implementation Guide](#client-implementation-guide)
9. [API Reference](#api-reference)
10. [Error Handling](#error-handling)
11. [Troubleshooting](#troubleshooting)

---

## Overview

The JARVIS Voice Assistant Server provides two main services:

1. **WebSocket Voice Assistant** - Real-time voice interaction with STT → LLM → TTS pipeline
2. **HTTP REST API** - Device management and Firebase Cloud Messaging (FCM) for push notifications

### Key Features

✅ Real-time voice processing with low latency  
✅ Multi-LLM support (Gemini, Cerebras)  
✅ Device registration and management  
✅ Push notifications for location requests and commands  
✅ Comprehensive session statistics and analytics  
✅ Token-based authentication  
✅ Support for multiple device types (phone, watch)

---

## Server Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    JARVIS Server                            │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  WebSocket Server (Port 8080)    HTTP API (Port 8000)      │
│  ├─ Voice Sessions               ├─ Device Registration    │
│  ├─ Audio Streaming               ├─ FCM Notifications     │
│  ├─ STT (Cartesia)               ├─ Location Requests      │
│  ├─ LLM (Gemini/Cerebras)        ├─ Command Execution      │
│  └─ TTS (Cartesia)               └─ Device Management      │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### Components

- **JarvisServer** - Main server managing both WebSocket and HTTP
- **VoiceSession** - Handles individual voice assistant sessions
- **DeviceManager** - Manages FCM device registration and tokens
- **FCMNotificationService** - Sends push notifications via Firebase
- **HTTPAPIServer** - REST API for device operations
- **CartesiaSTTClient** - Speech-to-text processing
- **CartesiaTTSClient** - Text-to-speech synthesis
- **LLM Clients** - AI response generation

---

## Getting Started

### Prerequisites

```bash
# Install Python dependencies
pip install -r requirements.txt
```

**Required Environment Variables:**
```bash
export CARTESIA_API_KEY="your_cartesia_key"
export GEMINI_API_KEY="your_gemini_key"        # Optional
export CEREBRAS_API_KEY="your_cerebras_key"    # Optional
export JARVIS_AUTH_TOKEN="your_auth_token"     # Optional
```

**Firebase Setup:**
- Place your Firebase service account JSON file in the project root
- Default name: `jarvis-app-43084-firebase-adminsdk-fbsvc-c836619551.json`

### Starting the Server

**Basic:**
```bash
python server.py
```

**Custom Configuration:**
```bash
python server.py \
  --host 0.0.0.0 \
  --port 8080 \
  --http-port 8000 \
  --llm cerebras \
  --token your_secret_token
```

**Options:**
- `--host` - Host to bind to (default: 0.0.0.0)
- `--port` - WebSocket port (default: 8080)
- `--http-port` - HTTP API port (default: 8000)
- `--llm` - LLM provider: `cerebras` or `gemini` (default: cerebras)
- `--token` - Authentication token (default: from env or "Denemeler123.")
- `--no-fcm` - Disable FCM functionality

**Server Output:**
```
============================================================
  JARVIS VOICE ASSISTANT SERVER
============================================================

  Host: 0.0.0.0
  WebSocket Port: 8080
  HTTP API Port: 8000
  LLM Provider: cerebras
  Auth Token: Dene...23.

  WebSocket URL: ws://0.0.0.0:8080?token=<your_token>
  HTTP API URL: http://0.0.0.0:8000/api

  Stats File: session_stats.jsonl

  FCM: Enabled
    - Device Registration: POST /api/register-device
    - Request Location: POST /api/request-location/{device_id}
    - Execute Command: POST /api/execute-command/{device_id}
    - List Devices: GET /api/devices

============================================================

Waiting for connections...
```

---

## WebSocket Voice Assistant API

### Connection

**URL:** `ws://SERVER_IP:8080?token=YOUR_TOKEN`

**Authentication:** Required via query parameter `token`

### Message Protocol

All JSON messages follow this format:
```json
{
  "type": "MESSAGE_TYPE",
  "data": { ... }
}
```

### Message Types

#### Client → Server

**1. SESSION_START** - Start a new voice session
```json
{
  "type": "session_start",
  "client_type": "phone",  // or "watch", "demo"
  "timestamp": "2026-01-06T10:00:00Z"
}
```

**2. AUDIO** - Send audio data
```json
{
  "type": "audio",
  "data": "base64_encoded_audio",
  "timestamp": "2026-01-06T10:00:01Z"
}
```
*Note: Binary audio can also be sent directly as raw bytes*

**3. END_OF_SPEECH** - Signal end of user speech
```json
{
  "type": "end_of_speech",
  "timestamp": "2026-01-06T10:00:05Z"
}
```

**4. CANCEL** - Cancel current session
```json
{
  "type": "cancel"
}
```

**5. PING** - Keep connection alive
```json
{
  "type": "ping"
}
```

#### Server → Client

**1. SESSION_STARTED** - Session initialized
```json
{
  "type": "session_started",
  "session_id": "abc12345",
  "timestamp": "2026-01-06T10:00:00Z"
}
```

**2. SESSION_READY** - Ready to receive audio
```json
{
  "type": "session_ready",
  "message": "Ready for audio"
}
```

**3. TRANSCRIPT** - Speech-to-text result
```json
{
  "type": "transcript",
  "text": "Hello Jarvis",
  "is_final": false,
  "timestamp": "2026-01-06T10:00:03Z"
}
```

**4. PROCESSING** - Processing stage update
```json
{
  "type": "processing",
  "stage": "llm",
  "message": "Generating response..."
}
```

**5. RESPONSE_TEXT** - LLM response chunk
```json
{
  "type": "response_text",
  "text": "Hello! How can I",
  "chunk_index": 1
}
```

**6. AUDIO_CHUNK** - TTS audio response (binary)
*Sent as raw PCM audio bytes (16-bit, 24kHz, mono)*

**7. DONE** - Session complete
```json
{
  "type": "done",
  "transcript": "Hello Jarvis",
  "response": "Hello! How can I help you today?",
  "stats": { ... }
}
```

**8. STATS** - Detailed session statistics
```json
{
  "type": "stats",
  "session_id": "abc12345",
  "timestamps": { ... },
  "latencies": { ... },
  "metrics": { ... }
}
```

**9. ERROR** - Error occurred
```json
{
  "type": "error",
  "message": "Error description",
  "timestamp": "2026-01-06T10:00:00Z"
}
```

**10. PONG** - Response to ping
```json
{
  "type": "pong"
}
```

### Audio Format

**Input (STT):**
- Encoding: PCM S16LE (signed 16-bit little-endian)
- Sample Rate: 16000 Hz
- Channels: Mono
- Chunk Size: Recommended 1024-4096 bytes

**Output (TTS):**
- Encoding: PCM S16LE
- Sample Rate: 24000 Hz
- Channels: Mono

### Voice Session Flow

```
Client                                Server
  │                                     │
  ├──── SESSION_START ────────────────> │
  │ <──── SESSION_STARTED ─────────────┤
  │ <──── SESSION_READY ───────────────┤
  │                                     │
  ├──── AUDIO (binary) ───────────────> │
  ├──── AUDIO (binary) ───────────────> │
  │ <──── TRANSCRIPT (partial) ────────┤
  ├──── AUDIO (binary) ───────────────> │
  │ <──── TRANSCRIPT (final) ──────────┤
  │                                     │
  ├──── END_OF_SPEECH ────────────────> │
  │                                     │
  │ <──── PROCESSING ──────────────────┤
  │ <──── RESPONSE_TEXT (chunk 1) ─────┤
  │ <──── AUDIO_CHUNK (binary) ────────┤
  │ <──── RESPONSE_TEXT (chunk 2) ─────┤
  │ <──── AUDIO_CHUNK (binary) ────────┤
  │ <──── DONE ────────────────────────┤
  │ <──── STATS ───────────────────────┤
  │                                     │
```

---

## HTTP REST API (FCM & Device Management)

**Base URL:** `http://SERVER_IP:8000/api`

All endpoints accept and return JSON unless otherwise specified.

---

## Device Registration Flow

### Step 1: Device Generates FCM Token

Your Android/WearOS app automatically generates an FCM token on first launch.

### Step 2: Register with Server

**Endpoint:** `POST /api/register-device`

**Request:**
```json
{
  "device_id": "phone_user123",
  "device_type": "phone",
  "fcm_token": "eX7gH...kL9mN",
  "app_version": "1.0.0",
  "os_version": "Android 14",
  "timestamp": "2026-01-06T21:00:00Z"
}
```

**Response:**
```json
{
  "status": "success",
  "device_id": "phone_user123",
  "registered_at": "2026-01-06T21:00:00Z"
}
```

### Step 3: Server Stores Device Info

The server maintains an in-memory registry of all devices with their FCM tokens.

---

## Push Notification Actions

### Action: Get Location

**Server sends:**
```json
{
  "notification": {
    "title": "Location Request",
    "body": "Server requesting your location"
  },
  "data": {
    "action": "get_location",
    "priority": "high",
    "accuracy": "high",
    "request_id": "loc_phone_user123_1704571200"
  }
}
```

**Device should respond to:**
`POST http://SERVER_IP:8000/api/location-update`

```json
{
  "device_id": "phone_user123",
  "latitude": 37.7749,
  "longitude": -122.4194,
  "accuracy": 10.0,
  "altitude": 50.0,
  "speed": 0.0,
  "bearing": 45.0,
  "timestamp": "2026-01-06T21:05:00Z"
}
```

### Action: Execute Command

**Server sends:**
```json
{
  "data": {
    "action": "execute_command",
    "command": "start_mic",
    "request_id": "cmd_phone_user123_1704571200"
  }
}
```

**Available Commands:**
- `start_mic` - Start microphone recording
- `stop_mic` - Stop microphone
- `vibrate` - Vibrate device
- `start` - Start JARVIS service
- `stop` - Stop JARVIS service

**Device should respond to:**
`POST http://SERVER_IP:8000/api/device-response`

```json
{
  "device_id": "phone_user123",
  "request_id": "cmd_phone_user123_1704571200",
  "action": "execute_command",
  "status": "success",
  "data": {
    "command": "start_mic",
    "executed_at": "2026-01-06T21:05:00Z"
  },
  "timestamp": "2026-01-06T21:05:01Z"
}
```

---

## Client Implementation Guide

### Android/WearOS Client

#### 1. Firebase Setup (Already Done)

```kotlin
// Your JarvisFCMService.kt handles incoming messages
class JarvisFCMService : FirebaseMessagingService() {
    override fun onMessageReceived(remoteMessage: RemoteMessage) {
        val action = remoteMessage.data["action"]
        when (action) {
            "get_location" -> handleLocationRequest(remoteMessage.data)
            "execute_command" -> handleCommand(remoteMessage.data)
        }
    }
}
```

#### 2. Device Registration

```kotlin
// On app start or token refresh
FirebaseMessaging.getInstance().token.addOnCompleteListener { task ->
    if (task.isSuccessful) {
        val token = task.result
        registerDeviceWithServer(token)
    }
}

fun registerDeviceWithServer(fcmToken: String) {
    val deviceInfo = JSONObject().apply {
        put("device_id", Settings.Secure.getString(contentResolver, Settings.Secure.ANDROID_ID))
        put("device_type", "phone") // or "watch"
        put("fcm_token", fcmToken)
        put("app_version", BuildConfig.VERSION_NAME)
        put("os_version", "Android ${Build.VERSION.RELEASE}")
        put("timestamp", System.currentTimeMillis())
    }
    
    // POST to http://SERVER_IP:8000/api/register-device
    sendHttpRequest("http://158.180.35.23:8000/api/register-device", deviceInfo)
}
```

#### 3. Handle Location Request

```kotlin
fun handleLocationRequest(data: Map<String, String>) {
    val requestId = data["request_id"]
    val priority = data["priority"]
    
    // Get location
    fusedLocationClient.lastLocation.addOnSuccessListener { location ->
        if (location != null) {
            sendLocationToServer(location, requestId)
        }
    }
}

fun sendLocationToServer(location: Location, requestId: String?) {
    val locationData = JSONObject().apply {
        put("device_id", getDeviceId())
        put("latitude", location.latitude)
        put("longitude", location.longitude)
        put("accuracy", location.accuracy)
        put("altitude", location.altitude)
        put("speed", location.speed)
        put("bearing", location.bearing)
        put("timestamp", System.currentTimeMillis())
        requestId?.let { put("request_id", it) }
    }
    
    // POST to http://SERVER_IP:8000/api/location-update
    sendHttpRequest("http://158.180.35.23:8000/api/location-update", locationData)
}
```

#### 4. Handle Commands

```kotlin
fun handleCommand(data: Map<String, String>) {
    val command = data["command"]
    val requestId = data["request_id"]
    
    val result = when (command) {
        "start_mic" -> startMicrophone()
        "stop_mic" -> stopMicrophone()
        "vibrate" -> vibrateDevice()
        "start" -> startJarvisService()
        "stop" -> stopJarvisService()
        else -> false
    }
    
    sendCommandResponse(command, result, requestId)
}

fun sendCommandResponse(command: String, success: Boolean, requestId: String?) {
    val response = JSONObject().apply {
        put("device_id", getDeviceId())
        put("action", "execute_command")
        put("status", if (success) "success" else "failed")
        put("data", JSONObject().apply {
            put("command", command)
            put("executed_at", System.currentTimeMillis())
        })
        put("timestamp", System.currentTimeMillis())
        requestId?.let { put("request_id", it) }
    }
    
    // POST to http://SERVER_IP:8000/api/device-response
    sendHttpRequest("http://158.180.35.23:8000/api/device-response", response)
}
```

---

## API Reference

### Device Management

#### Register Device
```
POST /api/register-device
```

**Body:**
```json
{
  "device_id": "string (required)",
  "device_type": "phone|watch (required)",
  "fcm_token": "string (required)",
  "app_version": "string (optional)",
  "os_version": "string (optional)",
  "timestamp": "ISO 8601 timestamp (optional)"
}
```

**Response:** `200 OK`
```json
{
  "status": "success",
  "device_id": "string",
  "registered_at": "ISO 8601 timestamp"
}
```

---

#### List All Devices
```
GET /api/devices
```

**Response:** `200 OK`
```json
{
  "devices": {
    "device_id_1": {
      "device_type": "phone",
      "app_version": "1.0.0",
      "os_version": "Android 14",
      "registered_at": "2026-01-06T21:00:00Z",
      "last_seen": "2026-01-06T21:05:00Z",
      "is_active": true
    }
  }
}
```

---

#### Get Device Info
```
GET /api/devices/{device_id}
```

**Response:** `200 OK`
```json
{
  "device_id": "string",
  "device_type": "phone",
  "app_version": "1.0.0",
  "os_version": "Android 14",
  "registered_at": "2026-01-06T21:00:00Z",
  "last_seen": "2026-01-06T21:05:00Z",
  "is_active": true
}
```

**Error:** `404 Not Found`
```json
{
  "error": "Device not found"
}
```

---

#### Get Statistics
```
GET /api/stats
```

**Response:** `200 OK`
```json
{
  "total_devices": 5,
  "active_devices": 4,
  "phones": 3,
  "watches": 1
}
```

---

#### Deactivate Device
```
DELETE /api/devices/{device_id}
```

**Response:** `200 OK`
```json
{
  "status": "deactivated",
  "device_id": "string"
}
```

---

### Push Notifications

#### Request Location from Device
```
POST /api/request-location/{device_id}?priority=high
```

**Query Parameters:**
- `priority` - `high` or `normal` (default: `high`)

**Response:** `200 OK`
```json
{
  "status": "sent",
  "device_id": "string",
  "message_id": "string"
}
```

---

#### Request Location from All Devices
```
POST /api/request-location-all?priority=normal
```

**Response:** `200 OK`
```json
{
  "status": "sent",
  "success_count": 4,
  "failure_count": 0
}
```

---

#### Execute Command on Device
```
POST /api/execute-command/{device_id}
```

**Body:**
```json
{
  "command": "start_mic|stop_mic|vibrate|start|stop"
}
```

**Response:** `200 OK`
```json
{
  "status": "sent",
  "device_id": "string",
  "message_id": "string"
}
```

---

#### Send Custom Notification
```
POST /api/send-notification/{device_id}
```

**Body:**
```json
{
  "title": "Custom Title",
  "body": "Custom message",
  "data": {
    "custom_key": "custom_value"
  }
}
```

**Response:** `200 OK`
```json
{
  "status": "sent",
  "device_id": "string",
  "message_id": "string"
}
```

---

#### Send to Device Type
```
POST /api/send-to-type/{device_type}
```

**Path Parameters:**
- `device_type` - `phone` or `watch`

**Body:**
```json
{
  "title": "Message Title",
  "body": "Message body",
  "data": {
    "action": "custom_action"
  }
}
```

**Response:** `200 OK`
```json
{
  "status": "sent",
  "success_count": 3,
  "failure_count": 0
}
```

---

#### Broadcast to All Devices
```
POST /api/broadcast
```

**Body:**
```json
{
  "title": "Broadcast Title",
  "body": "Message for all devices",
  "data": {
    "action": "system_update"
  }
}
```

**Response:** `200 OK`
```json
{
  "status": "sent",
  "success_count": 5,
  "failure_count": 0
}
```

---

### Device Responses

#### Location Update
```
POST /api/location-update
```

**Body:**
```json
{
  "device_id": "string (required)",
  "latitude": "number (required)",
  "longitude": "number (required)",
  "accuracy": "number (optional)",
  "altitude": "number (optional)",
  "speed": "number (optional)",
  "bearing": "number (optional)",
  "timestamp": "ISO 8601 timestamp (optional)"
}
```

**Response:** `200 OK`
```json
{
  "status": "received",
  "device_id": "string"
}
```

---

#### Generic Device Response
```
POST /api/device-response
```

**Body:**
```json
{
  "device_id": "string (required)",
  "request_id": "string (optional)",
  "action": "string (required)",
  "status": "success|failed (required)",
  "data": "object (optional)",
  "timestamp": "ISO 8601 timestamp (optional)"
}
```

**Response:** `200 OK`
```json
{
  "status": "acknowledged"
}
```

---

## Error Handling

### HTTP Error Codes

- **200 OK** - Request successful
- **400 Bad Request** - Invalid request body or parameters
- **404 Not Found** - Device or resource not found
- **500 Internal Server Error** - Server error

### WebSocket Errors

**Connection Refused:**
- Check authentication token
- Verify server is running
- Check network connectivity

**Message Format Errors:**
```json
{
  "type": "error",
  "message": "Invalid message format",
  "timestamp": "2026-01-06T10:00:00Z"
}
```

---

## Troubleshooting

### Common Issues

#### 1. Device Registration Fails

**Problem:** HTTP 400 Bad Request

**Solution:**
- Ensure `device_id` and `fcm_token` are included
- Check JSON formatting
- Verify server is running on correct port

**Test:**
```bash
curl -X POST http://SERVER_IP:8000/api/register-device \
  -H "Content-Type: application/json" \
  -d '{
    "device_id": "test_device",
    "device_type": "phone",
    "fcm_token": "test_token"
  }'
```

---

#### 2. Push Notifications Not Received

**Problem:** Device doesn't receive FCM messages

**Checklist:**
- ✓ Device is registered (check `/api/devices`)
- ✓ FCM token is valid and current
- ✓ Firebase service account JSON is correct
- ✓ Device has internet connection
- ✓ FCM service is running on device

**Debug:**
```bash
# Check device registration
curl http://SERVER_IP:8000/api/devices

# Try sending test notification
curl -X POST http://SERVER_IP:8000/api/send-notification/YOUR_DEVICE_ID \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Test",
    "body": "Testing notification",
    "data": {"test": "true"}
  }'
```

---

#### 3. WebSocket Connection Fails

**Problem:** Cannot connect to WebSocket

**Solution:**
- Check authentication token in URL
- Verify server is running on port 8080
- Check firewall rules

**Test:**
```javascript
// Browser console test
const ws = new WebSocket('ws://SERVER_IP:8080?token=YOUR_TOKEN');
ws.onopen = () => console.log('Connected');
ws.onerror = (e) => console.error('Error:', e);
```

---

#### 4. Location Updates Not Working

**Problem:** Location data not reaching server

**Solution:**
- Check device has location permissions
- Verify location services are enabled
- Ensure correct server URL in app
- Check network connectivity

**Server logs should show:**
```
[Location] device_id: 37.7749, -122.4194
```

---

#### 5. Firebase Initialization Fails

**Problem:** Server shows "FCM initialization failed"

**Solution:**
- Verify `jarvis-app-43084-firebase-adminsdk-fbsvc-c836619551.json` exists
- Check file permissions (readable)
- Validate JSON structure
- Ensure project_id matches

**Test:**
```python
import firebase_admin
from firebase_admin import credentials

cred = credentials.Certificate("jarvis-app-43084-firebase-adminsdk-fbsvc-c836619551.json")
firebase_admin.initialize_app(cred)
print("Firebase initialized successfully")
```

---

## Testing Commands

### Device Registration
```bash
curl -X POST http://158.180.35.23:8000/api/register-device \
  -H "Content-Type: application/json" \
  -d '{
    "device_id": "phone_test",
    "device_type": "phone",
    "fcm_token": "YOUR_FCM_TOKEN",
    "app_version": "1.0.0",
    "os_version": "Android 14",
    "timestamp": "2026-01-06T21:00:00Z"
  }'
```

### Request Location
```bash
# Single device
curl -X POST http://158.180.35.23:8000/api/request-location/phone_test

# All devices
curl -X POST http://158.180.35.23:8000/api/request-location-all
```

### Execute Command
```bash
curl -X POST http://158.180.35.23:8000/api/execute-command/phone_test \
  -H "Content-Type: application/json" \
  -d '{"command": "vibrate"}'
```

### List Devices
```bash
curl http://158.180.35.23:8000/api/devices
```

### Get Statistics
```bash
curl http://158.180.35.23:8000/api/stats
```

### Send to All Phones
```bash
curl -X POST http://158.180.35.23:8000/api/send-to-type/phone \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Test",
    "body": "Testing all phones",
    "data": {"action": "test"}
  }'
```

---

## Performance Metrics

### Voice Assistant Latency

- **Time to First Transcript:** ~200-500ms
- **STT Processing:** ~300-800ms
- **Time to First LLM Token:** ~200-600ms (Cerebras), ~400-1000ms (Gemini)
- **Time to First Audio:** ~300-700ms
- **Voice Response Latency:** ~1000-2500ms (end-to-end)

**Rating:**
- ⭐⭐⭐ Excellent: <1.5s
- ⭐⭐ Good: 1.5-2.5s
- ⭐ Needs improvement: >2.5s

### Push Notification Latency

- **FCM Delivery (High Priority):** ~100-500ms
- **FCM Delivery (Normal Priority):** ~1-30 seconds

---

## Security Considerations

1. **Authentication:** All WebSocket connections require token
2. **FCM Tokens:** Stored in memory only (not persisted)
3. **HTTPS:** Use HTTPS/WSS in production
4. **Rate Limiting:** Implement rate limiting for production
5. **Token Rotation:** Support FCM token refresh

---

## Support

For issues or questions:
- Check server logs for detailed error messages
- Review this documentation
- Test with curl commands
- Verify Firebase configuration

**Server Log Locations:**
- Session statistics: `session_stats.jsonl`
- Console output: stdout/stderr

---

**End of Documentation**
