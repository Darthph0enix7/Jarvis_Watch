# 📍 JARVIS Location Tracker Dashboard

A modern, real-time web dashboard for tracking GPS locations from your JARVIS client devices.

## 🌟 Features

- **Real-time Location Updates** - WebSocket connection for live location updates
- **Interactive Map** - Leaflet.js powered map with markers and path visualization
- **Timeline View** - See your location history with timestamps
- **Device Filtering** - Filter locations by specific devices
- **Customizable Limit** - Choose how many locations to display (10-500)
- **Statistics** - Track total locations, devices, accuracy, and more
- **Modern UI** - Beautiful, responsive design with smooth animations

## 🚀 Quick Start

### 1. Start the Server

The server should already be running. If not:

```bash
python server.py --host 0.0.0.0 --port 8000 --llm cerebras
```

### 2. Access the Dashboard

**Local Access:**
```
http://localhost:8000/
```

**Network Access (same network):**
```
http://<your-server-ip>:8000/
```

**Tailscale Access (from anywhere):**
```
http://<your-tailscale-ip>:8000/
```

To find your Tailscale IP:
```bash
tailscale ip
```

### 3. Get Your Tailscale IP

```bash
tailscale ip -4
```

Then access from any device on your Tailscale network:
```
http://100.x.x.x:8000/
```

## 📡 API Endpoints

The dashboard uses these API endpoints:

### Get Locations
```bash
# Get last 20 locations (all devices)
curl http://localhost:8000/api/locations?limit=20

# Get locations for specific device
curl http://localhost:8000/api/locations/YOUR_DEVICE_ID?limit=20
```

### Request Location Update
```bash
# Request location from all devices
curl -X POST http://localhost:8000/api/request-location-all

# Request location from specific device
curl -X POST http://localhost:8000/api/request-location/YOUR_DEVICE_ID
```

### List Devices
```bash
curl http://localhost:8000/api/devices
```

## 🎨 Dashboard Features

### Main Map View
- **Blue markers** - Historical locations
- **Green marker** - Latest location
- **Dashed line** - Path connecting locations in chronological order
- Click any marker for detailed information

### Controls
- **Show Dropdown** - Select how many locations to display
- **Refresh Button** - Manually reload locations
- **Request Update Button** - Send FCM request to devices for new location

### Statistics Panel
- **Total Locations** - Number of location records shown
- **Total Devices** - Number of unique devices
- **Average Accuracy** - Average GPS accuracy in meters
- **Last Update** - Time since last location update

### Device Filter
- Click on device to filter locations for that device only
- Click "All Devices" to show all locations

### Location Timeline
- **Green background** - Latest location
- **Gray background** - Historical locations
- **Click on any location** - Map will focus on that location
- Shows coordinates, accuracy, speed, and relative time

## 🔄 Real-Time Updates

The dashboard automatically:
- Connects via WebSocket for real-time updates
- Updates map and timeline when new locations arrive
- Shows connection status in header
- Reconnects automatically if disconnected

## 📊 Understanding Location Data

Each location includes:
- **Device ID** - Unique identifier for the device
- **Coordinates** - Latitude and longitude (6 decimal places)
- **Accuracy** - GPS accuracy in meters (lower is better)
- **Altitude** - Height above sea level in meters
- **Speed** - Movement speed in km/h
- **Bearing** - Direction of movement in degrees
- **Provider** - GPS provider (fused, gps, network)
- **Timestamp** - When the location was recorded

## 🛠️ Customization

### Change Map Provider

Edit `dashboard.html` and modify the tile layer:

```javascript
// OpenStreetMap (default)
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {...}).addTo(map);

// Dark mode
L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {...}).addTo(map);

// Satellite
L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {...}).addTo(map);
```

### Adjust Auto-Refresh Interval

Edit the interval in `dashboard.html`:

```javascript
// Default: 30 seconds
setInterval(updateStats, 30000);

// Change to 60 seconds
setInterval(updateStats, 60000);
```

## 🔒 Security Notes

Currently, the dashboard is accessible without authentication. For production use:

1. **Add authentication** to the dashboard route
2. **Use HTTPS** for encrypted communication
3. **Restrict access** via firewall rules
4. **Use Tailscale** for secure remote access

## 📱 Mobile Friendly

The dashboard is responsive and works on:
- Desktop browsers
- Tablets
- Mobile phones

## 🐛 Troubleshooting

### Dashboard shows no locations
1. Check if devices are registered: `curl http://localhost:8000/api/devices`
2. Request location update: `curl -X POST http://localhost:8000/api/request-location-all`
3. Check if `device_locations.jsonl` has data: `tail device_locations.jsonl`

### WebSocket won't connect
1. Check server is running: `ps aux | grep server.py`
2. Check server logs for errors
3. Verify port 8000 is not blocked by firewall

### Map doesn't load
1. Check browser console for errors (F12)
2. Verify internet connection (map tiles load from external CDN)
3. Try refreshing the page

### Locations not updating in real-time
1. Check WebSocket connection status (header badge)
2. Verify `ws://` URL is accessible
3. Some networks block WebSocket connections

## 📈 Performance

The dashboard is optimized for:
- Up to 500 locations without performance issues
- Multiple devices
- Real-time updates every few seconds
- Mobile data usage (map tiles are cached)

## 🎯 Use Cases

- **Personal Tracking** - Track your own movements
- **Family Safety** - Monitor family member locations
- **Asset Tracking** - Track vehicles or equipment
- **Geofencing** - Set up location-based alerts (extend with custom code)
- **Route Analysis** - Analyze travel patterns and routes

## 📝 Data Storage

Locations are stored in `device_locations.jsonl`:
- One JSON object per line
- Each line is a complete location record
- Easy to parse and analyze
- Can be imported into databases or analytics tools

Example location record:
```json
{
  "device_id": "ebb0a9e9aca70c1e",
  "device_type": "phone",
  "latitude": 50.810573,
  "longitude": 6.140569,
  "accuracy": 7.87,
  "altitude": 255.0,
  "speed": 0,
  "bearing": null,
  "provider": "fused",
  "timestamp": 1767736157.476,
  "received_at": 1767736158.077,
  "datetime": "2026-01-06T21:49:17.476000"
}
```

## 🔮 Future Enhancements

Potential features to add:
- Geofencing alerts
- Location history heatmap
- Export to GPX/KML formats
- Speed and distance statistics
- Battery level tracking
- Custom markers per device
- Location sharing links
- Historical playback mode

## 💡 Tips

1. **Increase location tracking frequency** - Adjust the interval in server startup:
   ```bash
   python server.py --location-interval 5  # Every 5 minutes
   ```

2. **View all historical data** - Select "Last 500" in the dropdown

3. **Focus on specific device** - Click device in the sidebar

4. **Quick navigation** - Click locations in timeline to jump to them on map

5. **Check connection** - Green badge = connected, Red badge = disconnected

## 📞 Support

For issues or questions:
1. Check server logs: `tail -f server.log` (if logging enabled)
2. Verify device registration: `curl http://localhost:8000/api/devices`
3. Test API endpoints with curl
4. Check browser console (F12) for JavaScript errors

---

**Built with:** Python, aiohttp, WebSockets, Leaflet.js, HTML5, CSS3

**Compatible with:** JARVIS Voice Assistant Server v1.0+
