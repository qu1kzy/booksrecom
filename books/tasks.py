import re
import logging
from decimal import Decimal, InvalidOperation
from celery import shared_task

logger = logging.getLogger(__name__)


def _parse_price(text: str):
    """
    Извлечь числовое значение цены из произвольной строки.

    Удаляет пробелы между разрядами и приводит число к Decimal,
    возвращает None, если цену определить не удалось.
    """
    text = re.sub(r"(\d)\s+(\d)", r"\1\2", text.strip())
    m = re.search(r"\d+[.,]?\d*", text)
    if not m:
        return None
    try:
        return Decimal(m.group().replace(",", "."))
    except InvalidOperation:
        return None


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def scrape_book_prices(self, book_id: int):
    """
    Спарсить актуальные цены книги во всех активных магазинах.

    Обновляет текущие цены в BookStore, пишет историю BookPrice
    и пересчитывает среднюю цену книги.
    """
    import requests
    from bs4 import BeautifulSoup
    from django.conf import settings
    from django.utils import timezone
    from books.models import Book, BookStore, BookPrice

    try:
        book = Book.objects.get(pk=book_id)
    except Book.DoesNotExist:
        return

    links = (
        BookStore.objects
        .filter(book=book, store__is_active=True)
        .exclude(product_url="")
        .select_related("store")
    )

    headers = {"User-Agent": settings.SCRAPER_USER_AGENT}
    prices  = []

    for link in links:
        selector = link.store.price_selector.strip()
        if not selector:
            continue
        try:
            resp = requests.get(link.product_url, headers=headers,
                                timeout=settings.SCRAPER_TIMEOUT)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")
            el   = soup.select_one(selector)
            if el is None:
                link.in_stock = False
                link.last_checked = timezone.now()
                link.save(update_fields=["in_stock", "last_checked"])
                continue

            price = _parse_price(el.get_text())
            if price is None:
                continue

            # Обновляем текущую цену
            link.current_price = price
            link.in_stock      = True
            link.last_checked  = timezone.now()
            link.save(update_fields=["current_price", "in_stock", "last_checked"])

            # Сохраняем в историю
            BookPrice.objects.create(book_store=link, price=price)

            prices.append(price)
            logger.info("Book #%d @ %s: %s ₽", book_id, link.store.name, price)

        except requests.RequestException as exc:
            logger.error("Book #%d @ %s: %s", book_id, link.store.name, exc)

    # Пересчитываем среднюю цену и дату
    if prices:
        book.avg_price = sum(prices) / len(prices)
    book.price_last_requested = timezone.now()
    book.save(update_fields=["avg_price", "price_last_requested"])


@shared_task
def extract_tags_from_description(book_id: int):
    """Извлечь AI-теги из описания книги при её создании."""
    from django.conf import settings as conf
    if not getattr(conf, "ANTHROPIC_API_KEY", ""):
        return

    from books.models import Book
    from books.tag_extraction import apply_tag_to_book
    import anthropic

    try:
        book = Book.objects.prefetch_related("authors", "genres").get(pk=book_id)
    except Book.DoesNotExist:
        return

    if not book.description:
        return

    authors = ", ".join(a.name for a in book.authors.all())
    genres  = ", ".join(g.name for g in book.genres.all())

    prompt = (
        f"Книга: «{book.title}» ({authors}). Жанры: {genres}.\n"
        f"Описание: {book.description[:500]}\n\n"
        "Выдели 3 характерные черты этой книги из её описания. "
        "Каждая черта — содержательная характеристика (атмосфера, тема, стиль, темп, эпоха). "
        "Не оценочные суждения. Каждая черта: 2-4 слова на русском, без точки. "
        "Ответь ТОЛЬКО тремя строками, по одной черте на строку. "
        "Пример:\nатмосфера тревоги\nисторический детектив\nмедленный темп"
    )

    try:
        client = anthropic.Anthropic(api_key=conf.ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=60,
            messages=[{"role": "user", "content": prompt}],
        )
        lines = msg.content[0].text.strip().splitlines()
        for line in lines[:3]:
            tag = line.strip().strip("«»\"'").strip()
            if 2 <= len(tag) <= 80:
                apply_tag_to_book(book, tag)
        logger.info("Description tags extracted for book #%d", book_id)
    except Exception as exc:
        logger.error("extract_tags_from_description error for book #%d: %s", book_id, exc)


@shared_task
def check_price_alerts():
    """Celery Beat: проверяем алерты цен раз в сутки."""
    from django.utils import timezone
    from books.models import PriceAlert
    from notifications.telegram import send_message
    from django.conf import settings as conf

    site_url = getattr(conf, "SITE_URL", "")
    now = timezone.now()

    alerts = (
        PriceAlert.objects
        .filter(triggered_at__isnull=True)
        .select_related("user__profile", "book")
    )

    for alert in alerts:
        book = alert.book
        if book.avg_price is None:
            continue
        if book.avg_price <= alert.threshold:
            profile = getattr(alert.user, "profile", None)
            if profile and profile.telegram_chat_id:
                book_url = f"{site_url}/books/{book.pk}/"
                text = (
                    f"🔔 <b>Цена снизилась!</b>\n\n"
                    f"<b>{book.title}</b>\n"
                    f"Текущая цена: {book.avg_price} ₽\n"
                    f"Ваш порог: {alert.threshold} ₽\n"
                )
                if site_url:
                    text += f"\n<a href='{book_url}'>Открыть в 'Строка'</a>"
                send_message(profile.telegram_chat_id, text)

            alert.triggered_at = now
            alert.save(update_fields=["triggered_at"])
            logger.info(
                "Price alert triggered: user %s, book #%d, price %s <= %s",
                alert.user.username, book.pk, book.avg_price, alert.threshold
            )
