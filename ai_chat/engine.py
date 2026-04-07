from django.conf import settings
from openai import OpenAI
from reviews.models import Review
from books.models import BookTag


def build_book_context(book):
    """Собирает контекст книги для AI."""
    parts = []

    # Основная информация
    authors = ", ".join(a.name for a in book.authors.all())
    genres = ", ".join(g.name for g in book.genres.all())
    parts.append(f"Название: {book.title}")
    if authors:
        parts.append(f"Авторы: {authors}")
    if genres:
        parts.append(f"Жанры: {genres}")
    if book.publication_year:
        parts.append(f"Год: {book.publication_year}")
    if book.series:
        parts.append(f"Серия: {book.series.name}")
    if book.pages:
        parts.append(f"Страниц: {book.pages}")

    # Описание
    if book.description:
        parts.append(f"\nОписание:\n{book.description}")

    # Контент от администратора
    try:
        content = book.ai_content
        if content.content_text:
            parts.append(f"\nДополнительная информация:\n{content.content_text}")
    except Exception:
        pass

    # Теги
    tags = BookTag.objects.filter(book=book).order_by("-count")[:10]
    if tags:
        parts.append(f"\nТеги: {', '.join(t.name for t in tags)}")

    # Отзывы
    reviews = Review.objects.filter(book=book, status=Review.APPROVED).order_by("-created_at")[:10]
    if reviews:
        parts.append("\nОтзывы читателей:")
        for r in reviews:
            text = r.text[:300] + "..." if len(r.text) > 300 else r.text
            parts.append(f"- ★{r.rating}: {text}")

    return "\n".join(parts)


def ask_about_book(chat, user_message):
    """Отправляет вопрос AI и возвращает ответ."""
    from .models import BookChatMessage

    book_context = build_book_context(chat.book)

    # История (последние 20 сообщений)
    history = list(
        chat.messages.order_by("-created_at")[:20]
    )
    history.reverse()

    messages = [
        {
            "role": "system",
            "content": (
                f'Ты — AI-рецензент и собеседник по книге. '
                f'Отвечай на русском языке. Обсуждай сюжет, персонажей, '
                f'темы, стиль автора и контекст. Если не знаешь — так и скажи.\n\n'
                f'Информация о книге:\n{book_context}'
            ),
        }
    ]

    for msg in history:
        messages.append({"role": msg.role, "content": msg.content})

    messages.append({"role": "user", "content": user_message})

    # Сохраняем сообщение пользователя
    BookChatMessage.objects.create(chat=chat, role="user", content=user_message)

    try:
        client = OpenAI(
            api_key=settings.ANTHROPIC_API_KEY,
            base_url=settings.ANTHROPIC_BASE_URL,
        )
        response = client.chat.completions.create(
            model="claude-haiku-4-5-20251001",
            messages=messages,
            max_tokens=1024,
        )
        ai_text = response.choices[0].message.content.strip()
    except Exception as e:
        ai_text = f"Извините, произошла ошибка при обращении к AI: {e}"

    # Сохраняем ответ AI
    BookChatMessage.objects.create(chat=chat, role="assistant", content=ai_text)

    return ai_text
