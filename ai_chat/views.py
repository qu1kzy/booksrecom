from django.shortcuts import render, get_object_or_404
from django.http import HttpResponse
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST, require_GET

from books.models import Book
from .models import BookChat, BookChatMessage, DiscoveryChat
from .engine import ask_about_book
from .discovery_engine import ask_discovery


@login_required
def book_chat(request, book_id):
    book = get_object_or_404(Book.objects.prefetch_related("authors"), pk=book_id)
    chat, _ = BookChat.objects.get_or_create(user=request.user, book=book)
    messages = chat.messages.order_by("created_at")[:100]
    return render(request, "ai_chat/book_chat.html", {
        "book": book,
        "chat": chat,
        "messages": messages,
    })


@login_required
@require_POST
def book_chat_send(request, book_id):
    book = get_object_or_404(Book.objects.prefetch_related("authors"), pk=book_id)
    chat, _ = BookChat.objects.get_or_create(user=request.user, book=book)
    user_message = request.POST.get("message", "").strip()

    if not user_message:
        return HttpResponse("")

    ai_text = ask_about_book(chat, user_message)

    # Возвращаем 2 последних сообщения (user + assistant)
    return render(request, "ai_chat/_messages.html", {
        "new_messages": [
            {"role": "user", "content": user_message},
            {"role": "assistant", "content": ai_text},
        ],
    })


@login_required
@require_POST
def book_chat_clear(request, book_id):
    book = get_object_or_404(Book, pk=book_id)
    BookChat.objects.filter(user=request.user, book=book).delete()
    return HttpResponse('<p style="font-size:13px;color:var(--ghost);text-align:center;padding:24px 0">История очищена. Задайте вопрос о книге.</p>')


# ─── DISCOVERY CHAT ──────────────────────────────────────────────────────────

@login_required
def discovery_chat(request):
    """Полная страница discovery-чата."""
    chat = DiscoveryChat.objects.filter(user=request.user).first()
    messages = []
    if chat:
        messages = list(chat.messages.order_by("created_at")[:50])
    return render(request, "ai_chat/discovery.html", {
        "chat": chat,
        "messages": messages,
    })


@login_required
@require_POST
def discovery_send(request):
    """HTMX: отправить сообщение discovery-чату."""
    user_message = request.POST.get("message", "").strip()
    if not user_message:
        return HttpResponse("")

    chat, _ = DiscoveryChat.objects.get_or_create(user=request.user)
    result = ask_discovery(request.user, user_message, chat)

    return render(request, "ai_chat/_discovery_response.html", {
        "text": result["text"],
        "books": result["books"],
    })


@login_required
@require_POST
def discovery_clear(request):
    """Очистить историю discovery-чата."""
    DiscoveryChat.objects.filter(user=request.user).delete()
    return HttpResponse(
        '<p style="font-size:13px;color:var(--ghost);text-align:center;'
        'padding:24px 0">Начните новый диалог. Опишите, какую книгу ищете.</p>'
    )
