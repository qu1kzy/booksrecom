import json

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer


class ChatConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.room_id = self.scope["url_route"]["kwargs"]["room_id"]
        self.group_name = f"chat_{self.room_id}"
        user = self.scope["user"]

        if user.is_anonymous:
            await self.close()
            return

        is_participant = await self._is_participant(user.pk, self.room_id)
        if not is_participant:
            await self.close()
            return

        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive(self, text_data):
        data = json.loads(text_data)
        body = data.get("body", "").strip()
        if not body:
            return

        user = self.scope["user"]
        msg = await self._save_message(user.pk, self.room_id, body)

        await self.channel_layer.group_send(
            self.group_name,
            {
                "type": "chat_message",
                "message_id": msg["id"],
                "body": msg["body"],
                "username": msg["username"],
                "created_at": msg["created_at"],
            },
        )

    async def chat_message(self, event):
        await self.send(text_data=json.dumps({
            "id": event["message_id"],
            "body": event["body"],
            "username": event["username"],
            "created_at": event["created_at"],
        }))

    @database_sync_to_async
    def _is_participant(self, user_id, room_id):
        from .models import ChatParticipant
        return ChatParticipant.objects.filter(user_id=user_id, room_id=room_id).exists()

    @database_sync_to_async
    def _save_message(self, user_id, room_id, body):
        from .models import ChatMessage
        msg = ChatMessage.objects.create(user_id=user_id, room_id=room_id, body=body)
        return {
            "id": msg.pk,
            "body": msg.body,
            "username": msg.user.username,
            "created_at": msg.created_at.strftime("%H:%M"),
        }
