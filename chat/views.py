from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db.models import Max, Q, OuterRef, Subquery
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .models import ChatMessage, ChatParticipant, ChatRoom

User = get_user_model()


def _get_or_create_dm(user_a, user_b):
    """Return the DM room between two users, creating one if needed."""
    room = (
        ChatRoom.objects.filter(room_type=ChatRoom.ROOM_DM, participants__user=user_a)
        .filter(participants__user=user_b)
        .first()
    )
    if room:
        return room
    room = ChatRoom.objects.create(room_type=ChatRoom.ROOM_DM)
    ChatParticipant.objects.create(room=room, user=user_a)
    ChatParticipant.objects.create(room=room, user=user_b)
    return room


@login_required
def chat_list(request):
    """List of user's chat rooms, sorted by last message."""
    rooms = (
        ChatRoom.objects.filter(participants__user=request.user)
        .annotate(last_msg_at=Max("messages__created_at"))
        .order_by("-last_msg_at")
    )

    last_msg_sub = Subquery(
        ChatMessage.objects.filter(room=OuterRef("pk")).order_by("-created_at").values("body")[:1]
    )
    rooms = rooms.annotate(last_msg_body=last_msg_sub)

    room_data = []
    for room in rooms:
        if room.room_type == ChatRoom.ROOM_DM:
            other = room.participants.exclude(user=request.user).select_related("user").first()
            title = other.user.username if other else "Чат"
        else:
            title = room.club.name if room.club else "Клубный чат"

        participant = room.participants.filter(user=request.user).first()
        unread = 0
        if participant:
            unread = room.messages.filter(created_at__gt=participant.last_read_at).exclude(user=request.user).count()

        room_data.append({
            "room": room,
            "title": title,
            "last_msg": room.last_msg_body or "",
            "unread": unread,
        })

    return render(request, "chat/chat_list.html", {"room_data": room_data})


@login_required
def chat_dm(request, user_id):
    """Open or create a DM with another user."""
    other = get_object_or_404(User, pk=user_id)
    if other == request.user:
        return redirect("chat_list")
    room = _get_or_create_dm(request.user, other)
    return redirect("chat_room", room_id=room.pk)


@login_required
def chat_room(request, room_id):
    """Render chat room page (WebSocket connects from JS)."""
    room = get_object_or_404(ChatRoom, pk=room_id)
    participant = room.participants.filter(user=request.user).first()
    if not participant:
        return redirect("chat_list")

    # mark as read
    from django.utils import timezone
    participant.last_read_at = timezone.now()
    participant.save(update_fields=["last_read_at"])

    messages = room.messages.select_related("user").order_by("created_at")[:100]

    if room.room_type == ChatRoom.ROOM_DM:
        other = room.participants.exclude(user=request.user).select_related("user").first()
        title = other.user.username if other else "Чат"
    else:
        title = room.club.name if room.club else "Клубный чат"

    return render(request, "chat/chat_room.html", {
        "room": room,
        "title": title,
        "chat_messages": messages,
    })


@login_required
@require_POST
def chat_edit_message(request, message_id):
    from django.utils import timezone
    msg = get_object_or_404(ChatMessage, pk=message_id, user=request.user)
    body = request.POST.get("body", "").strip()
    if not body:
        return JsonResponse({"error": "empty"}, status=400)
    msg.body = body
    msg.save(update_fields=["body"])
    return JsonResponse({"ok": True, "id": msg.pk, "body": msg.body})


@login_required
def chat_history(request, room_id):
    """HTMX partial: load older messages."""
    room = get_object_or_404(ChatRoom, pk=room_id)
    if not room.participants.filter(user=request.user).exists():
        return JsonResponse({"error": "forbidden"}, status=403)

    before = request.GET.get("before")
    qs = room.messages.select_related("user").order_by("-created_at")
    if before:
        qs = qs.filter(pk__lt=before)
    msgs = list(qs[:30])
    msgs.reverse()
    return render(request, "chat/_messages_batch.html", {"messages": msgs, "user": request.user})
