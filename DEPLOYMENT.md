# Jarvis Voice Assistant — Koyeb Deployment Guide

Deploy your Jarvis server to Koyeb (serverless container platform) in minutes, with automatic scaling, health checks, and security.

---

## 🔐 Security Overview

Your application has **multiple layers of protection**:

1. **Token Auth** (JARVIS_AUTH_TOKEN)
   - Applied to ALL endpoints except `/health`
   - Protects voice WebSocket, API calls, and dashboard

2. **Dashboard HTTP Basic Auth** (Optional)
   - Extra username:password protection for web dashboard (`/` and `/dashboard`)
   - Set `DASHBOARD_USER` and `DASHBOARD_PASSWORD` environment variables

3. **HTTPS on Koyeb**
   - All traffic is encrypted with TLS
   - Koyeb provides a free wildcard certificate

---

## 📋 Prerequisites

1. **GitHub repository** containing this code (Koyeb builds from git)
2. **Koyeb account** (https://www.koyeb.com) — free tier available
3. **API keys** ready:
   - `JARVIS_AUTH_TOKEN` — any secret string (e.g., `Denemeler123.`)
   - `CARTESIA_API_KEY` — Cartesia STT/TTS (https://cartesia.ai)
   - `CEREBRAS_API_KEY` — Cerebras LLM (https://cerebras.ai)

---

## 🚀 Deployment via Koyeb Web Dashboard

### Step 1: Create a Koyeb Account & Log In
- Go to https://www.koyeb.com
- Sign up (free tier supports small deployments)
- Log in to the dashboard

### Step 2: Create a New Application

1. Click **"Create App"** or **"New Application"**
2. Select **"GitHub"** as the source (or upload a Git repository URL)
3. Authorize Koyeb to access your GitHub account
4. Select your repository containing this code

### Step 3: Configure the Service

**Name & Build Settings:**
- Service name: `jarvis` (or your preferred name)
- Build command: Leave as default (Koyeb auto-detects Dockerfile)
- Dockerfile path: `Dockerfile` (already in repo root)
- Build context: `.` (repo root)

**Runtime:**
- Instance type: `Nano` for testing; upgrade to `Small` if you get timeout errors
- Regions: Choose closest region (e.g., `sfo` for US West, `fra` for Europe)
- Port: `8000` ✓ (matches Dockerfile EXPOSE)

**Health Check:**
- Path: `/health`
- Port: `8000`
- Interval: `30s`
- Timeout: `5s`
- Startup delay: `10s` ✓

These are pre-configured in `koyeb.yaml`, but verify they match above.

### Step 4: Add Environment Variables & Secrets

**In the Koyeb dashboard, go to "Environment & Secrets" tab:**

#### Secrets (recommended — encrypted storage):
```
JARVIS_AUTH_TOKEN       = Denemeler123.
CARTESIA_API_KEY        = sk_car_...
CEREBRAS_API_KEY        = csk-...
```

Click **"+ Add Secret"** for each:
1. Name: `jarvis-auth-token`
2. Value: `Denemeler123.` (or your token)
3. Click **"Save"**

Repeat for `cartesia-api-key` and `cerebras-api-key`.

#### Environment Variables (non-sensitive):
```
PORT                    = 8000
LLM_PROVIDER            = cerebras
LOCATION_INTERVAL       = 30
```

(All optional; defaults are fine)

**Dashboard Protection** (optional, HTTP Basic Auth):
```
DASHBOARD_USER          = admin
DASHBOARD_PASSWORD      = yourpassword123
```

If you add these, you'll need to provide credentials when accessing `https://your-app.koyeb.app/`.

### Step 5: Bind Secrets to Environment Variables

For each secret you created, bind it to the matching environment variable:

**In the "Secrets" section:**
- Select secret `jarvis-auth-token`
- Bind to env var name: `JARVIS_AUTH_TOKEN`
- Click **"Bind"**

Repeat for `cartesia-api-key` → `CEREBRAS_API_KEY` and `cerebrass-api-key` → `CEREBRAS_API_KEY`.

### Step 6: Configure Auto-Scaling (Optional)

In the **"Scaling"** section:
- Min instances: `1`
- Max instances: `3` (or higher for production)
- Auto-scale trigger: CPU/Memory threshold

For a small deployment, leave at `1` instance.

### Step 7: Deploy!

Click **"Deploy"** or **"Create & Deploy"**

Koyeb will:
1. Clone your GitHub repo
2. Build the Docker image
3. Push to Koyeb's registry
4. Deploy and run health checks
5. Assign a public URL (e.g., `https://jarvis-abc123.koyeb.app`)

---

## ✅ Post-Deployment Verification

Once deployed, test these endpoints from your terminal or browser:

### 1. Health Check (no auth required)
```bash
curl https://your-app.koyeb.app/health
# Response: {"status": "ok"}
```

### 2. List Devices (requires token)
```bash
curl "https://your-app.koyeb.app/api/devices?token=Denemeler123."
```

### 3. Access Dashboard (requires token + optional Basic Auth)
```
https://your-app.koyeb.app/?token=Denemeler123.
```

If `DASHBOARD_USER` and `DASHBOARD_PASSWORD` are set, browser will prompt for username/password.

### 4. WebSocket Connection Test (requires token)
```bash
websocat -H "Authorization: Bearer Denemeler123." wss://your-app.koyeb.app/ws
```

Or use a WebSocket client app to connect to `wss://your-app.koyeb.app/ws?token=Denemeler123.`

---

## 🔄 Updating Your Deployment

To redeploy with code changes:

1. Push updated code to your GitHub branch
2. Koyeb auto-triggers a rebuild (check **"Deployments"** tab)
3. Once built, the new version goes live

To disable auto-deploy, go to **Settings** → **GitHub Integration** → disable **"Auto Deploy"**

---

## 📝 Key Environment Variables Reference

| Variable | Required? | Example | Description |
|---|---|---|---|
| `JARVIS_AUTH_TOKEN` | ✓ | `Denemeler123.` | Shared secret for client auth + Firebase creds decryption |
| `CARTESIA_API_KEY` | ✓ | `sk_car_...` | Cartesia STT/TTS |
| `CEREBRAS_API_KEY` | ✓ | `csk-...` | Cerebras LLM (if using Cerebras provider) |
| `GEMINI_API_KEY` | ✗ | `AIza...` | Google Gemini API (only if `LLM_PROVIDER=gemini`) |
| `LLM_PROVIDER` | ✗ | `cerebras` | Which LLM to use: `cerebras` (default) or `gemini` |
| `PORT` | ✗ | `8000` | HTTP port (Koyeb sets this; rarely change it) |
| `LOCATION_INTERVAL` | ✗ | `30` | Minutes between GPS location polls |
| `FIREBASE_SERVICE_ACCOUNT` | ✗ | `...json.enc` | Path to Firebase creds (defaults to bundled `.enc` file) |
| `DASHBOARD_USER` | ✗ | `admin` | HTTP Basic Auth username for dashboard (enable with `DASHBOARD_PASSWORD`) |
| `DASHBOARD_PASSWORD` | ✗ | `pass123` | HTTP Basic Auth password for dashboard (enable with `DASHBOARD_USER`) |

---

## 🚨 Important Notes

1. **Ephemeral Filesystem**
   - Device locations (`device_locations.jsonl`) and session stats (`session_stats.jsonl`) are stored locally
   - They reset when the container restarts or redeploys
   - For persistence, integrate a database (Supabase, PostgreSQL, etc.)

2. **Firebase Credentials**
   - The repo includes an **encrypted** copy: `jarvis-app-43084-...firebase-adminsdk-fbsvc....json.enc`
   - It's decrypted at startup using `JARVIS_AUTH_TOKEN`
   - The plain `.json` file is in `.gitignore` and never committed
   - To use different Firebase credentials:
     - Encrypt them: `JARVIS_AUTH_TOKEN=<token> python encrypt_creds.py path/to/new/creds.json`
     - Commit the `.enc` file to git

3. **Scaling Considerations**
   - Each instance is stateless (except for in-memory session maps)
   - WebSocket connections may drop if request is routed to a different instance
   - Device location data is not replicated across instances
   - For production, add a distributed cache (Redis) or message queue (RabbitMQ)

4. **Health Check Behavior**
   - `/health` endpoint returns `{"status": "ok"}` with no authentication
   - Koyeb uses this to detect if the app is alive
   - Unhealthy instances are automatically restarted

---

## 🆘 Troubleshooting

**App crashes on startup:**
- Check logs: Koyeb dashboard → **Logs** tab
- Likely cause: Missing environment variable (esp. API keys)
- Solution: Verify all secrets are bound to env vars (see Step 5)

**WebSocket connection refused:**
- Ensure you're using `wss://` (not `ws://`) for Koyeb  
- Verify token is passed via query param or Bearer header
- Check that `/health` responds (if not, instance is unhealthy)

**Health check failing:**
- The `/health` endpoint should always return `{"status": "ok"}` within 5 seconds
- If it takes longer, increase the timeout in Koyeb dashboard
- App may need more time to initialize; increase **Startup delay** to `15-20s`

**Location data disappears after redeploy:**
- This is expected — locations are stored in the container's local filesystem
- Redeploy restarts the container, wiping local files
- Solution: Integrate a persistent database (PostgreSQL, Supabase, etc.)

---

## 📚 Additional Resources

- **Koyeb Docs:** https://www.koyeb.com/docs
- **Docker Docs:** https://docs.docker.com
- **aiohttp (server library):** https://docs.aiohttp.org
- **Cartesia (STT/TTS):** https://docs.cartesia.ai
- **Cerebras (LLM):** https://docs.cerebras.com

---

## 🎉 You're Live!

Once deployed, your API is accessible at:

```
https://jarvis-<your-id>.koyeb.app
```

- **Voice WebSocket:** `wss://jarvis-<your-id>.koyeb.app/ws?token=<token>`
- **Location Dashboard:** `https://jarvis-<your-id>.koyeb.app/?token=<token>`
- **API Endpoints:** `https://jarvis-<your-id>.koyeb.app/api/*?token=<token>`

You can now integrate this with your client apps (smartwatch, mobile app, voice devices, etc.) using the public URL and token! 🚀
