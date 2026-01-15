"""
Location Tracking Module

Handles device location tracking and storage.
"""

import asyncio
import json
import time
from typing import Optional
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


# ============================================================================
# LOCATION TRACKING
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
    
    def __init__(self, fcm_service, device_manager, location_tracker: LocationTracker,
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
                await asyncio.sleep(self.interval_minutes * 60)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[LocationTracker] Error in periodic task: {e}")
                await asyncio.sleep(60)
    
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
                
                last_seen_str = device_info.get('last_seen')
                if last_seen_str:
                    last_seen = datetime.fromisoformat(last_seen_str)
                    time_since_seen = (datetime.now() - last_seen).total_seconds()
                    
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
