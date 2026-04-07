"""
Discovery Engine — AI-чат для поиска книг без привязки к конкретной книге.
Пользователь описывает, что хочет прочитать, AI рекомендует из каталога.
"""

import json
import logging

from django.conf import settings
from django.contrib.postgres.search import SearchVector, SearchRank, SearchQuery
from django.db.models import Q
from openai import OpenAI

from books.models import Book, BookMood, UserList
from books.ai_recommendations import build_user_context
from .models import DiscoveryChat, DiscoveryChatMessage

logger = logging.getLogger(__name__)


def _search_catalog(query: str, limit: int = 30) -> list[Book]:
    """Поиск книг по запросу (FTS + trigram + жанры/теги)."""
    # FTS
    search_query = SearchQuery(query, config="russian")
    search_vector = SearchVector("title", weight="A", config="russian") + \
                    SearchVector("description", weight="C", config="russian")

    fts_results = (
        Book.objects
        .annotate(rank=SearchRank(search_vector, search_query))
        .filter(rank__gte=0.05)
        .order_by("-rank")[:limit]
    )
    results = list(fts_results.prefetch_related("authors", "genres"))

    # Если FTS дал мало — дополним популярными
    if len(results) < limit:
        remaining = limit - len(results)
        seen_ids = {b.pk for b in results}
        extra = (
            Book.objects
            .exclude(pk__in=seen_ids)
            .prefetch_related("authors", "genres")
            .order_by("-avg_rating", "-rating_count")[:remaining]
        )
        results.extend(extra)

    return results


def _build_candidates_text(books: list[Book]) -> str:
    """Формирует пронумерованный список книг для Claude."""
    lines = []
    for i, book in enumerate(books, 1):
        authors = ", ".join(a.name for a in book.authors.all())
        genres = ", ".join(g.name for g in book.genres.all())
        moods = ", ".join(
            bm.mood.name for bm in BookMood.objects.filter(book=book).select_related("mood")[:5]
        )
        line = f"{i}. «{book.title}»"
        if authors:
            line += f" — {authors}"
        if genres:
            line += f" [{genres}]"
        if moods:
            line += f" ({moods})"
        if book.description:
            line += f": {book.description[:150]}"
        lines.append(line)
    return "\n".join(lines)


def _build_user_profile(user) -> str:
    """Краткий профиль пользователя для промпта."""
    parts = []
    profile = getattr(user, "profile", None)
    if profile:
        fav_genres = list(profile.favorite_genres.all())
        fav_authors = list(profile.favorite_authors.all())
        if fav_genres:
            parts.append(f"Любимые жанры: {', '.join(g.name for g in fav_genres)}")
        if fav_authors:
            parts.append(f"Любимые авторы: {', '.join(a.name for a in fav_authors)}")

    # Последние книги из позитивных списков
    positive_lists = UserList.objects.filter(
        user=user, sentiment_tag="positive"
    ).prefetch_related("books__authors")[:3]
    recent_books = []
    for ul in positive_lists:
        for b in ul.books.all()[:5]:
            recent_books.append(f"«{b.title}»")
    if recent_books:
        parts.append(f"Недавно понравилось: {', '.join(recent_books[:10])}")

    return "\n".join(parts) if parts else "Новый пользователь, предпочтения неизвестны."


def ask_discovery(user, message: str, chat: DiscoveryChat) -> dict:
    """
    Основной вызов: пользователь описывает запрос, AI рекомендует книги.
    Возвращает: {"text": str, "books": [Book]}
    """
    # Ищем книги по запросу
    candidates = _search_catalog(message, limit=30)
    candidates_text = _build_candidates_text(candidates)
    user_profile = _build_user_profile(user)

    # История чата
    history = list(chat.messages.order_by("-created_at")[:10])
    history.reverse()

    messages = [
        {
            "role": "system",
            "content": (
                "Ты — книжный советник на русском языке. Помогаешь найти книгу по описанию. "
                "Рекомендуй ТОЛЬКО из предоставленного каталога. "
                "Используй инструмент recommend_books для структурированного ответа. "
                "Объясни, почему каждая книга подходит. "
                "Если запрос слишком расплывчатый — задай уточняющий вопрос.\n\n"
                f"Профиль пользователя:\n{user_profile}\n\n"
                f"Доступный каталог (книги пронумерованы 1-{len(candidates)}):\n{candidates_text}"
            ),
        }
    ]

    for msg in history:
        messages.append({"role": msg.role, "content": msg.content})
    messages.append({"role": "user", "content": message})

    # Сохраняем сообщение пользователя
    DiscoveryChatMessage.objects.create(chat=chat, role="user", content=message)

    tools = [{
        "type": "function",
        "function": {
            "name": "recommend_books",
            "description": "Рекомендовать книги пользователю",
            "parameters": {
                "type": "object",
                "properties": {
                    "explanation": {
                        "type": "string",
                        "description": "Общий текст ответа пользователю",
                    },
                    "books": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "index": {"type": "integer", "description": "Номер книги из каталога (1-N)"},
                                "reason": {"type": "string", "description": "Почему книга подходит"},
                            },
                            "required": ["index", "reason"],
                        },
                    },
                },
                "required": ["explanation", "books"],
            },
        },
    }]

    try:
        client = OpenAI(
            api_key=settings.ANTHROPIC_API_KEY,
            base_url=settings.ANTHROPIC_BASE_URL,
        )
        response = client.chat.completions.create(
            model="claude-haiku-4-5-20251001",
            messages=messages,
            tools=tools,
            max_tokens=1500,
        )
    except Exception as exc:
        logger.error("Discovery AI error: %s", exc)
        ai_msg = DiscoveryChatMessage.objects.create(
            chat=chat, role="assistant",
            content="Извините, произошла ошибка. Попробуйте позже.",
        )
        return {"text": ai_msg.content, "books": []}

    # Парсим ответ
    choice = response.choices[0]
    recommended_books = []
    explanation = ""

    if choice.message.tool_calls:
        for tc in choice.message.tool_calls:
            if tc.function.name == "recommend_books":
                try:
                    data = json.loads(tc.function.arguments)
                    explanation = data.get("explanation", "")
                    for item in data.get("books", []):
                        idx = item.get("index", 0) - 1
                        if 0 <= idx < len(candidates):
                            recommended_books.append({
                                "book": candidates[idx],
                                "reason": item.get("reason", ""),
                            })
                except (json.JSONDecodeError, KeyError) as exc:
                    logger.warning("Discovery parse error: %s", exc)

    if not explanation and choice.message.content:
        explanation = choice.message.content

    if not explanation:
        explanation = "Вот что я нашёл:"

    # Сохраняем ответ
    ai_msg = DiscoveryChatMessage.objects.create(
        chat=chat, role="assistant", content=explanation,
    )
    if recommended_books:
        ai_msg.recommended_books.set([rb["book"] for rb in recommended_books])

    return {
        "text": explanation,
        "books": recommended_books,
    }
