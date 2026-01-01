# Jarvis Voice Assistant - Protocol Specification

**Version:** 1.0  
**Last Updated:** December 31, 2024

This document describes the complete WebSocket protocol for building clients (Android, WearOS, iOS, web) that communicate with the Jarvis Voice Assistant server.

---

## Table of Contents

1. [Overview](#overview)
2. [Connection](#connection)
3. [Authentication](#authentication)
4. [Audio Format Specifications](#audio-format-specifications)
5. [Message Protocol](#message-protocol)
6. [Session Lifecycle](#session-lifecycle)
7. [Statistics and Metrics](#statistics-and-metrics)
8. [Client Implementation Guide](#client-implementation-guide)
9. [Error Handling](#error-handling)
10. [Code Examples](#code-examples)

---

## Overview

The Jarvis Voice Assistant uses a **WebSocket-based** bidirectional protocol for real-time voice communication. The architecture is:

```
┌─────────────────┐           ┌─────────────────┐
│     Client      │           │     Server      │
│  (Watch/Phone)  │◄─────────►│    (Jarvis)     │
│                 │ WebSocket │                 │
│  - Mic capture  │           │  - STT (Speech) │
│  - Audio play   │           │  - LLM (Brain)  │
│  - UI display   │           │  - TTS (Voice)  │
└─────────────────┘           └─────────────────┘
```

### Key Features:
- **Real-time streaming**: Audio is streamed continuously, not sent all at once
- **Parallel processing**: LLM and TTS run in parallel for low latency
- **Two-way communication**: Both JSON control messages and binary audio
- **Live transcripts**: Speech-to-text results streamed in real-time

---

## Connection

### WebSocket URL Format

```
ws://<host>:<port>?token=<auth_token>
```

Or with TLS:
```
wss://<host>:<port>?token=<auth_token>
```

### Default Configuration
- **Host**: `localhost` (or server IP/domain)
- **Port**: `8080`
- **Protocol**: WebSocket (ws://) or Secure WebSocket (wss://)

### Connection Parameters
| Parameter | Required | Description |
|-----------|----------|-------------|
| `token` | Yes | Authentication token (shared secret) |

### Connection Example
```javascript
const ws = new WebSocket("ws://192.168.1.100:8080?token=your_secret_password");
```

---

## Authentication

The server uses **token-based authentication**. The token is passed as a URL query parameter during the WebSocket handshake.

### Server Configuration
The server expects the token to be set via environment variable or config:
```bash
export JARVIS_AUTH_TOKEN="your_secure_password_here"
python server.py
```

### Authentication Flow
1. Client connects with `?token=xxx` in URL
2. Server validates token during handshake
3. If invalid, connection is rejected with HTTP 401
4. If valid, WebSocket connection is established

### Security Notes
- Use **HTTPS/WSS** in production to encrypt the token
- Generate a strong random token (32+ characters)
- Consider rotating tokens periodically

---

## Audio Format Specifications

### Input Audio (Client → Server)

Audio data sent from client microphone to server.

| Property | Value |
|----------|-------|
| **Sample Rate** | 16,000 Hz |
| **Bit Depth** | 16-bit |
| **Channels** | 1 (Mono) |
| **Encoding** | PCM signed 16-bit little-endian (`pcm_s16le`) |
| **Chunk Size** | 512-1024 samples (~32-64ms) |
| **Byte Order** | Little-endian |

#### Calculating Chunk Size
- 512 samples × 2 bytes = **1024 bytes** per chunk
- At 16kHz: 1024 bytes = **32ms** of audio

### Output Audio (Server → Client)

Audio data sent from server TTS to client speaker.

| Property | Value |
|----------|-------|
| **Sample Rate** | 24,000 Hz |
| **Bit Depth** | 16-bit |
| **Channels** | 1 (Mono) |
| **Encoding** | PCM signed 16-bit little-endian (`pcm_s16le`) |

#### Android/WearOS AudioTrack Configuration
```kotlin
val audioTrack = AudioTrack.Builder()
    .setAudioAttributes(AudioAttributes.Builder()
        .setUsage(AudioAttributes.USAGE_ASSISTANT)
        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
        .build())
    .setAudioFormat(AudioFormat.Builder()
        .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
        .setSampleRate(24000)
        .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
        .build())
    .setTransferMode(AudioTrack.MODE_STREAM)
    .setBufferSizeInBytes(4800) // 100ms buffer
    .build()
```

---

## Message Protocol

Communication uses two types of data:
1. **JSON Messages** - Control messages, transcripts, metadata
2. **Binary Data** - Raw audio bytes

### Message Types

#### Client → Server Messages

| Type | Description | When to Send |
|------|-------------|--------------|
| `session_start` | Begin new voice session | When user activates assistant |
| `audio` (binary) | Raw audio bytes | Continuously during recording |
| `end_of_speech` | User finished speaking | When recording stops |
| `cancel` | Cancel current session | User cancels or navigates away |
| `ping` | Keepalive | Every 30 seconds |

#### Server → Client Messages

| Type | Description | Client Action |
|------|-------------|---------------|
| `session_started` | Session acknowledged | Record session_id |
| `session_ready` | Server ready for audio | Start sending audio |
| `transcript` | Live speech-to-text | Display transcript |
| `processing` | Status update | Show loading state |
| `response_text` | LLM text chunk | Display response text |
| `audio_response` | TTS audio chunk | Play audio |
| `done` | Session complete | End session, show stats |
| `error` | Error occurred | Handle error |
| `pong` | Keepalive response | Connection alive |
| `stats` | Session statistics | Log/display metrics |

### Message Schemas

#### `session_start` (Client → Server)
```json
{
    "type": "session_start",
    "client_type": "watch",        // "watch", "phone", "demo"
    "client_id": "device-uuid",    // Optional unique device ID
    "client_version": "1.0.0"      // Optional client version
}
```

#### `end_of_speech` (Client → Server)
```json
{
    "type": "end_of_speech"
}
```

#### `cancel` (Client → Server)
```json
{
    "type": "cancel"
}
```

#### `ping` (Client → Server)
```json
{
    "type": "ping",
    "timestamp": 1735654321000
}
```

#### `session_started` (Server → Client)
```json
{
    "type": "session_started",
    "session_id": "a1b2c3d4"
}
```

#### `session_ready` (Server → Client)
```json
{
    "type": "session_ready",
    "message": "Ready for audio"
}
```

#### `transcript` (Server → Client)
```json
{
    "type": "transcript",
    "text": "Hello, how are you",
    "is_final": false
}
```

Notes:
- `is_final: false` = partial transcript (still listening)
- `is_final: true` = final segment (committed)

#### `processing` (Server → Client)
```json
{
    "type": "processing",
    "stage": "llm",
    "message": "Generating response..."
}
```

Stages: `"stt"`, `"llm"`, `"tts"`

#### `response_text` (Server → Client)
```json
{
    "type": "response_text",
    "text": "I'm doing great, thanks for asking!",
    "chunk_index": 1
}
```

#### `audio_response` (Server → Client)
```json
{
    "type": "audio_response",
    "data": "BASE64_ENCODED_PCM_AUDIO",
    "sample_rate": 24000,
    "is_final": false
}
```

**Important**: Audio can also be sent as raw binary frames for efficiency.

#### `done` (Server → Client)
```json
{
    "type": "done",
    "transcript": "Hello, how are you?",
    "response": "I'm doing great, thanks for asking!",
    "stats": {
        "session_id": "a1b2c3d4",
        "timestamps": { ... },
        "latencies": { ... },
        "metrics": { ... }
    }
}
```

#### `error` (Server → Client)
```json
{
    "type": "error",
    "message": "STT connection failed",
    "code": "STT_ERROR"
}
```

Error codes:
- `AUTH_FAILED` - Authentication failed
- `STT_ERROR` - Speech-to-text error
- `LLM_ERROR` - LLM generation error
- `TTS_ERROR` - Text-to-speech error
- `TIMEOUT` - Session timeout
- `CANCELLED` - Session cancelled

#### `stats` (Server → Client)
```json
{
    "type": "stats",
    "session_id": "a1b2c3d4",
    "timestamps": {
        "session_start": 1735654321.123,
        "stt_connected": 1735654321.234,
        "first_audio_received": 1735654321.345,
        "speech_start": 1735654322.100,
        "first_transcript": 1735654322.450,
        "speech_end": 1735654324.500,
        "final_transcript": 1735654324.650,
        "llm_request_sent": 1735654324.660,
        "first_llm_token": 1735654324.900,
        "first_llm_chunk": 1735654325.100,
        "tts_connected": 1735654324.400,
        "first_tts_chunk_sent": 1735654325.110,
        "first_audio_response": 1735654325.400,
        "llm_complete": 1735654326.200,
        "last_audio_response": 1735654328.500,
        "session_end": 1735654328.510
    },
    "latencies": {
        "stt_connect_ms": 111,
        "time_to_first_transcript_ms": 350,
        "stt_processing_ms": 150,
        "time_to_first_llm_token_ms": 240,
        "time_to_first_llm_chunk_ms": 440,
        "tts_latency_ms": 290,
        "voice_response_latency_ms": 900,
        "total_response_latency_ms": 4010,
        "total_session_ms": 7387
    },
    "metrics": {
        "speech_duration_ms": 2400,
        "input_word_count": 5,
        "speaking_rate_wpm": 125,
        "output_word_count": 12,
        "output_char_count": 56,
        "llm_chunks_generated": 3,
        "audio_chunks_sent": 75,
        "audio_bytes_sent": 76800,
        "audio_chunks_received": 45,
        "audio_bytes_received": 115200,
        "llm_provider": "cerebras",
        "llm_model": "gpt-oss-120b"
    }
}
```

---

## Session Lifecycle

### Complete Session Flow

```
CLIENT                                     SERVER
  │                                          │
  │──── Connect (ws://...?token=xxx) ───────>│
  │                                          │ Validate token
  │<──────── Connection Accepted ────────────│
  │                                          │
  │──── {"type": "session_start"} ──────────>│
  │                                          │ Connect to STT
  │                                          │ Pre-connect TTS
  │<──── {"type": "session_started"} ────────│
  │<──── {"type": "session_ready"} ──────────│
  │                                          │
  │──── [Binary Audio Chunk 1] ─────────────>│ Forward to STT
  │──── [Binary Audio Chunk 2] ─────────────>│
  │<──── {"type": "transcript", ...} ────────│ Partial transcript
  │──── [Binary Audio Chunk 3] ─────────────>│
  │<──── {"type": "transcript", ...} ────────│ Updated transcript
  │──── [Binary Audio Chunk N] ─────────────>│
  │                                          │
  │──── {"type": "end_of_speech"} ──────────>│ Signal done talking
  │                                          │
  │<──── {"type": "transcript", final} ──────│ Final transcript
  │<──── {"type": "processing", "llm"} ──────│ Processing status
  │                                          │
  │                                          │ ┌─ LLM generates ─┐
  │                                          │ │ TTS converts    │
  │                                          │ │ (parallel)      │
  │                                          │ └─────────────────┘
  │                                          │
  │<──── {"type": "response_text"} ──────────│ Text chunk 1
  │<──── [Binary Audio Response] ────────────│ Audio for chunk 1
  │<──── {"type": "response_text"} ──────────│ Text chunk 2
  │<──── [Binary Audio Response] ────────────│ Audio for chunk 2
  │      ... more chunks ...                 │
  │<──── [Binary Audio Response] ────────────│ Final audio
  │                                          │
  │<──── {"type": "done", "stats": {...}} ───│ Session complete
  │                                          │
  │<──── {"type": "stats"} ──────────────────│ Detailed statistics
  │                                          │
```

### State Machine

```
┌─────────────┐
│ DISCONNECTED│
└──────┬──────┘
       │ connect()
       ▼
┌─────────────┐
│  CONNECTED  │
└──────┬──────┘
       │ send session_start
       ▼
┌─────────────┐
│   WAITING   │◄─────────┐
└──────┬──────┘          │
       │ receive session_ready
       ▼                 │
┌─────────────┐          │
│  RECORDING  │          │
└──────┬──────┘          │
       │ send end_of_speech
       ▼                 │
┌─────────────┐          │
│ PROCESSING  │          │
└──────┬──────┘          │
       │ receive done    │
       ▼                 │
┌─────────────┐          │
│   PLAYING   │          │
└──────┬──────┘          │
       │ audio complete  │
       └─────────────────┘
```

---

## Statistics and Metrics

### Latency Definitions

| Metric | Definition | Formula |
|--------|------------|---------|
| **STT Connect** | Time to establish STT WebSocket | `stt_connected - session_start` |
| **Time to First Transcript** | User starts speaking → first transcript | `first_transcript - speech_start` |
| **STT Processing Latency** | Silence detected → final transcript | `final_transcript - speech_end` |
| **Time to First LLM Token** | Request sent → first token received | `first_llm_token - llm_request_sent` |
| **Time to First LLM Chunk** | Request sent → first TTS chunk ready | `first_llm_chunk - llm_request_sent` |
| **TTS Latency** | Chunk sent → audio received | `first_audio_response - first_tts_chunk_sent` |
| **Voice Response Latency** ⭐ | User stops talking → first audio plays | `first_audio_response - speech_end` |
| **Total Session** | Full session duration | `session_end - session_start` |

### Key Performance Indicator

**Voice Response Latency** is the most important metric for user experience:
- ⭐⭐⭐ **Excellent**: < 1.5 seconds
- ⭐⭐ **Good**: < 2.5 seconds
- ⭐ **Acceptable**: < 4.0 seconds

### Statistics JSON Structure

```json
{
    "timestamps": {
        "session_start": 1735654321.123,
        "stt_connected": 1735654321.234,
        "tts_connected": 1735654321.345,
        "first_audio_received": 1735654321.400,
        "speech_start": 1735654322.100,
        "first_transcript": 1735654322.450,
        "speech_end": 1735654324.500,
        "final_transcript": 1735654324.650,
        "llm_request_sent": 1735654324.660,
        "first_llm_token": 1735654324.900,
        "first_llm_chunk": 1735654325.100,
        "first_tts_chunk_sent": 1735654325.110,
        "first_audio_response": 1735654325.400,
        "llm_complete": 1735654326.200,
        "last_audio_response": 1735654328.500,
        "session_end": 1735654328.510
    },
    "latencies": {
        "stt_connect_ms": 111,
        "tts_connect_ms": 222,
        "time_to_first_transcript_ms": 350,
        "stt_processing_ms": 150,
        "time_to_first_llm_token_ms": 240,
        "time_to_first_llm_chunk_ms": 440,
        "tts_latency_ms": 290,
        "voice_response_latency_ms": 900,
        "total_response_latency_ms": 4010,
        "total_session_ms": 7387
    },
    "metrics": {
        "speech_duration_ms": 2400,
        "input_word_count": 5,
        "speaking_rate_wpm": 125,
        "transcript_updates": 12,
        "final_segments": 2,
        "output_word_count": 45,
        "output_char_count": 234,
        "llm_chunks_generated": 5,
        "audio_chunks_sent": 75,
        "audio_bytes_sent": 76800,
        "audio_chunks_received": 120,
        "audio_bytes_received": 288000,
        "llm_provider": "cerebras",
        "llm_model": "gpt-oss-120b"
    }
}
```

---

## Client Implementation Guide

### Android/Kotlin Implementation Steps

#### 1. Add Dependencies (build.gradle.kts)
```kotlin
dependencies {
    implementation("org.java-websocket:Java-WebSocket:1.5.4")
    // or OkHttp WebSocket
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
}
```

#### 2. Create WebSocket Client
```kotlin
class JarvisClient(
    private val serverUrl: String,
    private val authToken: String
) {
    private var webSocket: WebSocket? = null
    private val okHttpClient = OkHttpClient.Builder()
        .pingInterval(30, TimeUnit.SECONDS)
        .build()
    
    fun connect() {
        val request = Request.Builder()
            .url("$serverUrl?token=$authToken")
            .build()
        
        webSocket = okHttpClient.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                // Connected!
            }
            
            override fun onMessage(webSocket: WebSocket, text: String) {
                handleJsonMessage(text)
            }
            
            override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
                handleAudioResponse(bytes.toByteArray())
            }
            
            override fun onFailure(ws: WebSocket, t: Throwable, response: Response?) {
                // Handle error
            }
        })
    }
}
```

#### 3. Start Session
```kotlin
fun startSession() {
    val message = JSONObject().apply {
        put("type", "session_start")
        put("client_type", "watch")  // or "phone"
    }
    webSocket?.send(message.toString())
}
```

#### 4. Send Audio (During Recording)
```kotlin
// Configure AudioRecord for 16kHz mono PCM
private val audioRecord = AudioRecord(
    MediaRecorder.AudioSource.MIC,
    16000,  // Sample rate
    AudioFormat.CHANNEL_IN_MONO,
    AudioFormat.ENCODING_PCM_16BIT,
    1024 * 2  // Buffer size
)

fun startRecording() {
    audioRecord.startRecording()
    
    thread {
        val buffer = ByteArray(1024)  // 512 samples × 2 bytes
        while (isRecording) {
            val bytesRead = audioRecord.read(buffer, 0, buffer.size)
            if (bytesRead > 0) {
                // Send raw binary audio
                webSocket?.send(ByteString.of(*buffer, 0, bytesRead))
            }
        }
    }
}
```

#### 5. End Speech
```kotlin
fun endSpeech() {
    isRecording = false
    audioRecord.stop()
    
    val message = JSONObject().apply {
        put("type", "end_of_speech")
    }
    webSocket?.send(message.toString())
}
```

#### 6. Handle Server Messages
```kotlin
fun handleJsonMessage(text: String) {
    val json = JSONObject(text)
    when (json.getString("type")) {
        "session_started" -> {
            sessionId = json.getString("session_id")
        }
        "session_ready" -> {
            // Start recording
            startRecording()
        }
        "transcript" -> {
            val transcript = json.getString("text")
            val isFinal = json.getBoolean("is_final")
            updateTranscriptUI(transcript, isFinal)
        }
        "processing" -> {
            showLoadingUI(json.getString("message"))
        }
        "response_text" -> {
            val text = json.getString("text")
            appendResponseText(text)
        }
        "done" -> {
            val stats = json.optJSONObject("stats")
            onSessionComplete(stats)
        }
        "stats" -> {
            val stats = json.getJSONObject("stats")
            logStatistics(stats)
        }
        "error" -> {
            showError(json.getString("message"))
        }
    }
}
```

#### 7. Play Audio Response
```kotlin
private var audioTrack: AudioTrack? = null

fun initAudioTrack() {
    audioTrack = AudioTrack.Builder()
        .setAudioAttributes(AudioAttributes.Builder()
            .setUsage(AudioAttributes.USAGE_ASSISTANT)
            .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
            .build())
        .setAudioFormat(AudioFormat.Builder()
            .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
            .setSampleRate(24000)  // Output sample rate!
            .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
            .build())
        .setTransferMode(AudioTrack.MODE_STREAM)
        .setBufferSizeInBytes(4800)
        .build()
    
    audioTrack?.play()
}

fun handleAudioResponse(audioBytes: ByteArray) {
    audioTrack?.write(audioBytes, 0, audioBytes.size)
}
```

### WearOS Considerations

1. **Battery**: Use WorkManager for background connections
2. **Network**: Handle WiFi vs Bluetooth connectivity
3. **UI**: Keep interactions simple, use voice feedback
4. **Permissions**: Request RECORD_AUDIO and INTERNET

```kotlin
// WearOS Manifest permissions
<uses-permission android:name="android.permission.RECORD_AUDIO" />
<uses-permission android:name="android.permission.INTERNET" />
<uses-permission android:name="android.permission.WAKE_LOCK" />
```

---

## Error Handling

### Connection Errors
```kotlin
override fun onFailure(ws: WebSocket, t: Throwable, response: Response?) {
    when {
        response?.code == 401 -> {
            // Authentication failed
            showAuthError()
        }
        t is SocketTimeoutException -> {
            // Connection timeout - retry
            scheduleReconnect()
        }
        else -> {
            // General error
            showConnectionError(t.message)
        }
    }
}
```

### Session Errors
```kotlin
when (errorCode) {
    "AUTH_FAILED" -> promptForNewToken()
    "STT_ERROR" -> retrySession()
    "LLM_ERROR" -> showFallbackResponse()
    "TTS_ERROR" -> showTextOnlyResponse()
    "TIMEOUT" -> promptRetry()
    "CANCELLED" -> reset()
}
```

### Reconnection Strategy
```kotlin
class ReconnectionManager {
    private var retryCount = 0
    private val maxRetries = 5
    private val baseDelayMs = 1000L
    
    fun scheduleReconnect() {
        if (retryCount < maxRetries) {
            val delay = baseDelayMs * (1 shl retryCount)  // Exponential backoff
            handler.postDelayed({ connect() }, delay)
            retryCount++
        } else {
            showPermanentError()
        }
    }
    
    fun onConnected() {
        retryCount = 0
    }
}
```

---

## Code Examples

### Minimal Python Client
```python
import asyncio
import websockets
import json
import pyaudio

async def main():
    uri = "ws://localhost:8080?token=your_secret"
    
    async with websockets.connect(uri) as ws:
        # Start session
        await ws.send(json.dumps({
            "type": "session_start",
            "client_type": "demo"
        }))
        
        # Wait for ready
        while True:
            msg = await ws.recv()
            data = json.loads(msg)
            if data["type"] == "session_ready":
                break
        
        # Send audio (simplified)
        p = pyaudio.PyAudio()
        stream = p.open(format=pyaudio.paInt16,
                       channels=1,
                       rate=16000,
                       input=True,
                       frames_per_buffer=512)
        
        # Record for 3 seconds
        for _ in range(int(16000 / 512 * 3)):
            audio = stream.read(512)
            await ws.send(audio)
        
        stream.stop_stream()
        stream.close()
        
        # Signal end
        await ws.send(json.dumps({"type": "end_of_speech"}))
        
        # Receive response
        while True:
            msg = await ws.recv()
            if isinstance(msg, bytes):
                # Play audio...
                pass
            else:
                data = json.loads(msg)
                print(f"[{data['type']}]", data)
                if data["type"] == "done":
                    break

asyncio.run(main())
```

### JavaScript/Web Client
```javascript
class JarvisClient {
    constructor(serverUrl, authToken) {
        this.ws = new WebSocket(`${serverUrl}?token=${authToken}`);
        this.audioContext = new AudioContext({ sampleRate: 24000 });
        this.setupHandlers();
    }
    
    setupHandlers() {
        this.ws.onmessage = (event) => {
            if (event.data instanceof Blob) {
                this.playAudio(event.data);
            } else {
                const msg = JSON.parse(event.data);
                this.handleMessage(msg);
            }
        };
    }
    
    startSession() {
        this.ws.send(JSON.stringify({
            type: 'session_start',
            client_type: 'web'
        }));
    }
    
    async sendAudio(audioBlob) {
        this.ws.send(audioBlob);
    }
    
    endSpeech() {
        this.ws.send(JSON.stringify({ type: 'end_of_speech' }));
    }
    
    handleMessage(msg) {
        switch (msg.type) {
            case 'transcript':
                document.getElementById('transcript').textContent = msg.text;
                break;
            case 'response_text':
                document.getElementById('response').textContent += msg.text + ' ';
                break;
            case 'done':
                console.log('Session stats:', msg.stats);
                break;
        }
    }
    
    async playAudio(blob) {
        const arrayBuffer = await blob.arrayBuffer();
        const audioBuffer = await this.audioContext.decodeAudioData(arrayBuffer);
        const source = this.audioContext.createBufferSource();
        source.buffer = audioBuffer;
        source.connect(this.audioContext.destination);
        source.start();
    }
}
```

---

## Appendix: Full Message Type Reference

```python
# Client → Server
SESSION_START = "session_start"
AUDIO = "audio"  # or raw binary
END_OF_SPEECH = "end_of_speech"
CANCEL = "cancel"
PING = "ping"

# Server → Client  
SESSION_STARTED = "session_started"
SESSION_READY = "session_ready"
TRANSCRIPT = "transcript"
PROCESSING = "processing"
RESPONSE_TEXT = "response_text"
AUDIO_RESPONSE = "audio_response"  # or raw binary
DONE = "done"
ERROR = "error"
PONG = "pong"
STATS = "stats"
```

---

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2024-12-31 | Initial specification |
