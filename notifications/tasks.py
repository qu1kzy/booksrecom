import logging
from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task
def notify_book_added(book_id: int):
    """
    Отправить Telegram-уведомления всем подписчикам авторов новой книги.
    Запускается из сигнала post_save на Book.
    """
    from books.models import Book
    from users.models import AuthorSubscription
    from notifications.telegram import send_message
    from django.conf import settings

    try:
        book = Book.objects.prefetch_related("authors").get(pk=book_id)
    except Book.DoesNotExist:
        return

    author_ids = list(book.authors.values_list("id", flat=True))
    if not author_ids:
        return

    # Подписчики с заполненным chat_id
    subs = (
        AuthorSubscription.objects
        .filter(author_id__in=author_ids)
        .select_related("user__profile", "author")
        .distinct()
    )

    site_url = settings.__dict__.get("SITE_URL", "")
    book_url  = f"{site_url}/books/{book.pk}/"

    sent_users = set()  # чтобы не слать дважды если подписан на нескольких авторов книги

    for sub in subs:
        profile = getattr(sub.user, "profile", None)
        if not profile or not profile.telegram_chat_id:
            continue
        if sub.user_id in sent_users:
            continue

        authors_str = ", ".join(a.name for a in book.authors.all())
        text = (
            f"📚 <b>Новая книга от {sub.author.name}</b>\n\n"
            f"<b>{book.title}</b>\n"
            f"Авторы: {authors_str}\n"
        )
        if book.publication_year:
            text += f"Год: {book.publication_year}\n"
        if site_url:
            text += f"\n<a href='{book_url}'>Открыть в Строка</a>"

        ok = send_message(profile.telegram_chat_id, text)
        if ok:
            sent_users.add(sub.user_id)
            logger.info("Notified user %s about book #%d", sub.user.username, book_id)
        else:
            logger.warning("Failed to notify user %s", sub.user.username)

    logger.info("Book #%d: notified %d users", book_id, len(sent_users))
