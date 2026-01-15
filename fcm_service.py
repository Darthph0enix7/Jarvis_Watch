"""
FCM (Firebase Cloud Messaging) & Device Management Module

Handles device registration, FCM push notifications, and device management.
"""

import time
from typing import Optional
from datetime import datetime

import firebase_admin
from firebase_admin import credentials, messaging


# ============================================================================
# DEVICE MANAGEMENT
# ============================================================================

class DeviceManager:
    """Manages FCM device registration and messaging."""
    
    def __init__(self, firebase_service_account: str):
        self.devices = {}  # device_id -> device_info
        self.token_to_device = {}  # fcm_token -> device_id
        self.pending_requests = {}  # request_id -> request_info
        
        # Initialize Firebase Admin SDK
        try:
            cred = credentials.Certificate(firebase_service_account)
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


# ============================================================================
# FCM NOTIFICATION SERVICE
# ============================================================================

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
            )
        )
        
        try:
            response = messaging.send(message)
            print(f"[FCM] Silent data sent to {device_id}: {response}")
            return {'status': 'sent', 'message_id': response, 'device_id': device_id}
        except Exception as e:
            print(f"[FCM] Failed to send to {device_id}: {e}")
            return {'status': 'failed', 'error': str(e), 'device_id': device_id}
