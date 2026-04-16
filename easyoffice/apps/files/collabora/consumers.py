"""
apps/files/collabora/consumers.py

A single WebSocket consumer that any page can connect to in order to
receive live presence updates for a specific file.

Protocol (server → client):
    { "type": "presence", "editors": [ ... ] }

The client just needs to connect; it never sends anything. Presence
broadcasts are triggered by the Django views whenever a session starts,
pings, or ends.
"""
import json
import logging

from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async


logger = logging.getLogger(__name__)


class CollaboraPresenceConsumer(AsyncWebsocketConsumer):
    """
    Path: /ws/files/<file_id>/presence/

    Users connect here from any page that wants to show live presence
    for the file (the file manager grid, or the editor page itself).
    """

    async def connect(self):
        user = self.scope.get('user')
        if not user or not user.is_authenticated:
            await self.close(code=4401)
            return

        self.file_id = self.scope['url_route']['kwargs']['file_id']

        # Authorize: must have at least view permission on the file.
        allowed = await self._has_access(user.id, self.file_id)
        if not allowed:
            await self.close(code=4403)
            return

        self.group_name = f'collabora_file_{self.file_id}'
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

        # Send initial snapshot so the UI populates immediately.
        snapshot = await self._build_snapshot()
        await self.send(json.dumps({'type': 'presence', 'editors': snapshot}))

    async def disconnect(self, close_code):
        group = getattr(self, 'group_name', None)
        if group:
            await self.channel_layer.group_discard(group, self.channel_name)

    # Group message handler — fires when a view calls group_send with
    # {'type': 'presence.update', ...}
    async def presence_update(self, event):
        snapshot = await self._build_snapshot()
        await self.send(json.dumps({'type': 'presence', 'editors': snapshot}))

    # ── DB helpers ──────────────────────────────────────────────────────────

    @database_sync_to_async
    def _has_access(self, user_id, file_id):
        from apps.files.models import SharedFile
        from apps.core.models import User
        from apps.files.views import _file_permission_for
        try:
            user = User.objects.get(pk=user_id)
            file = SharedFile.objects.get(pk=file_id)
        except (User.DoesNotExist, SharedFile.DoesNotExist):
            return False
        return _file_permission_for(user, file) is not None

    @database_sync_to_async
    def _build_snapshot(self):
        from apps.files.models import SharedFile
        from .models import CollaboraSession
        try:
            file = SharedFile.objects.get(pk=self.file_id)
        except SharedFile.DoesNotExist:
            return []

        editors = []
        for s in CollaboraSession.active_for_file(file):
            name = getattr(s.user, 'full_name', '') or s.user.get_username()
            parts = [p for p in name.split() if p]
            initials = (
                (parts[0][:2] if len(parts) == 1 else parts[0][:1] + parts[-1][:1])
                if parts else '?'
            ).upper()
            editors.append({
                'user_id':    s.user_id,
                'name':       name,
                'initials':   initials,
                'permission': s.permission,
                'since':      s.started_at.isoformat(),
            })
        return editors
