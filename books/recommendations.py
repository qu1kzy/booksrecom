"""
Рекомендательный движок на чистом PostgreSQL — без ML, без внешних зависимостей.

Алгоритм скоринга (похожие книги):
  +4 за каждого совпавшего автора
  +3 за каждый совпавший жанр (с TF-IDF весом — редкий жанр ценнее)
  +2 если та же серия
  +1 если год публикации ±5 лет
  +0.5 * avg_rating книги-кандидата (бонус за качество)

Персональные рекомендации:
  Собираем «профиль вкуса» из жанров/авторов книг пользователя,
  взвешиваем по частоте встречаемости и рейтингу оценок и sentiment_tag списка,
  применяем TF-IDF для редких жанров,
  находим непрочитанные книги с максимальным попаданием.

Коллаборативная фильтрация (also_read):
  Если пользователи A, B, C добавили книгу X и книгу Y в один список —
  значит Y похожа на X. Чистый SQL, без ML.
"""

import math
from django.core.cache import cache
from django.db.models import Q, Count

from .models import Book, UserList, Genre
from reviews.models import Review
# Веса по sentiment_tag списка
_SENTIMENT_WEIGHT = {
    "positive": 1.0,
    "wishlist": 0.6,
    "neutral": 0.4,
    "negative": -0.5,
}

_IDF_CACHE_KEY = "genre_idf_v1"
_IDF_CACHE_TTL = 60 * 60  # 1 час


# ── ПОХОЖИЕ КНИГИ ─────────────────────────────────────────────────────────────

def similar_books(book, limit=6):
    """
    Вернуть список книг, похожих на заданную.

    Похожесть считается по авторам, жанрам (c TF‑IDF весами),
    серии, году издания и среднему рейтингу.
    """

    genre_ids = list(book.genres.values_list("id", flat=True))
    author_ids = list(book.authors.values_list("id", flat=True))

    if not genre_ids and not author_ids:
        return list(
            Book.objects.exclude(pk=book.pk)
            .prefetch_related("authors", "genres")
            .order_by("-avg_rating")[:limit]
        )

    q = Q(genres__id__in=genre_ids) | Q(authors__id__in=author_ids)
    if book.series_id:
        q |= Q(series_id=book.series_id)

    candidates = (
        Book.objects.exclude(pk=book.pk)
        .filter(q)
        .distinct()
        .prefetch_related("authors", "genres")
    )

    idf = _genre_idf()
    scored = _score_books(candidates, genre_ids, author_ids, book, idf)
    scored.sort(key=lambda x: -x[0])

    result = [b for _, b in scored[:limit]]
    if len(result) < limit:
        seen = {b.pk for b in result} | {book.pk}
        extra = list(
            Book.objects.exclude(pk__in=seen)
            .prefetch_related("authors", "genres")
            .order_by("-avg_rating")[: limit - len(result)]
        )
        result += extra

    return result


def _genre_idf():
    """TF-IDF вес жанра — кешируется на 1 час."""
    cached = cache.get(_IDF_CACHE_KEY)
    if cached is not None:
        return cached

    total = max(Book.objects.count(), 1)
    counts = Genre.objects.annotate(book_count=Count("books")).values("id", "book_count")
    idf = {row["id"]: math.log(total / max(row["book_count"], 1)) for row in counts}
    cache.set(_IDF_CACHE_KEY, idf, _IDF_CACHE_TTL)
    return idf


def invalidate_idf_cache():
    """Вызывать при добавлении новой книги."""
    cache.delete(_IDF_CACHE_KEY)


def _score_books(candidates, genre_ids, author_ids, anchor_book=None, idf=None):
    """Скоринг кандидатов. Использует prefetch_related — без N+1."""
    genre_set = set(genre_ids)
    author_set = set(author_ids)
    idf = idf or {}
    scored = []

    for book in candidates:
        score = 0.0
        c_genres = {g.id for g in book.genres.all()}
        c_authors = {a.id for a in book.authors.all()}

        for gid in c_genres & genre_set:
            score += 3 * idf.get(gid, 1.0)

        score += len(c_authors & author_set) * 4

        if anchor_book and anchor_book.series_id and book.series_id == anchor_book.series_id:
            score += 2

        if anchor_book and anchor_book.publication_year and book.publication_year:
            if abs(anchor_book.publication_year - book.publication_year) <= 5:
                score += 1

        score += book.avg_rating * 0.5

        if score > 0:
            scored.append((score, book))

    return scored


# ── КОЛЛАБОРАТИВНАЯ ФИЛЬТРАЦИЯ (Также читают) ────────────────────────────────

def also_read(book, limit=6):
    """
    Item-based CF: находим пользователей у которых есть эта книга в любом
    НЕ-отрицательном списке, затем смотрим какие ещё книги они добавляли.
    """

    # Только списки с положительным или нейтральным тегом
    positive_list_ids = UserList.objects.exclude(
        sentiment_tag="negative"
    ).filter(books=book).values_list("id", flat=True)

    user_ids = (
        UserList.objects
        .filter(id__in=positive_list_ids)
        .values_list("user_id", flat=True)
        .distinct()
    )

    cf_books = list(
        Book.objects
        .filter(in_lists__user__in=user_ids)
        .exclude(pk=book.pk)
        .annotate(co_count=Count(
            "in_lists__user",
            filter=Q(in_lists__user__in=user_ids),
            distinct=True,
        ))
        .filter(co_count__gt=0)
        .order_by("-co_count", "-avg_rating")
        .prefetch_related("authors", "genres")
        [:limit]
    )

    result = cf_books

    if len(result) < limit:
        seen = {b.pk for b in result} | {book.pk}
        genre_ids = list(book.genres.values_list("id", flat=True))
        author_ids = list(book.authors.values_list("id", flat=True))
        if genre_ids or author_ids:
            q = Q(genres__id__in=genre_ids) | Q(authors__id__in=author_ids)
            candidates = (
                Book.objects
                .exclude(pk__in=seen)
                .filter(q).distinct()
                .prefetch_related("authors", "genres")
            )
            idf = _genre_idf()
            extra_scored = _score_books(candidates, genre_ids, author_ids, book, idf)
            extra_scored.sort(key=lambda x: -x[0])
            result += [b for _, b in extra_scored[: limit - len(result)]]

    return result


# ── ПЕРСОНАЛЬНЫЕ РЕКОМЕНДАЦИИ ─────────────────────────────────────────────────

def recommended_for_user(user, limit=10):

    list_books = (
        Book.objects
        .filter(in_lists__user=user)
        .select_related()
        .prefetch_related("genres", "authors")
        .values_list("id", "in_lists__sentiment_tag")
    )

    user_reviews = Review.objects.filter(user=user).values("book_id", "rating")
    reviewed = {r["book_id"]: r["rating"] for r in user_reviews}
    seen_ids = set(reviewed.keys())

    genre_weight = {}
    author_weight = {}

    # Взвешиваем по sentiment_tag списка и рейтингу отзыва
    book_sentiments = {}  # book_id → лучший вес из всех списков
    for book_id, sentiment in list_books:
        seen_ids.add(book_id)
        sw = _SENTIMENT_WEIGHT.get(sentiment or "neutral", 0.4)
        if sw > book_sentiments.get(book_id, -999):
            book_sentiments[book_id] = sw

    if not book_sentiments and not reviewed:
        return _cold_start(user, limit)

    anchor_ids = set(book_sentiments.keys()) | set(reviewed.keys())
    anchors = Book.objects.filter(pk__in=anchor_ids).prefetch_related("genres", "authors")

    for b in anchors:
        # Рейтинг из отзыва или нейтральный 5/5
        rating_factor = reviewed.get(b.pk, 5) / 5.0
        # Вес из sentiment_tag (положительные списки вносят больше, отрицательные — минус)
        sentiment_w = book_sentiments.get(b.pk, 0.4)
        weight = sentiment_w * rating_factor

        for g in b.genres.all():
            genre_weight[g.id] = genre_weight.get(g.id, 0) + weight
        for a in b.authors.all():
            author_weight[a.id] = author_weight.get(a.id, 0) + weight

    # TF-IDF: редкий жанр ценнее
    idf = _genre_idf()
    for gid in genre_weight:
        genre_weight[gid] *= idf.get(gid, 1.0)

    if not genre_weight and not author_weight:
        return _cold_start(user, limit)

    candidates = (
        Book.objects
        .exclude(pk__in=seen_ids)
        .filter(
            Q(genres__id__in=genre_weight.keys()) |
            Q(authors__id__in=author_weight.keys())
        )
        .distinct()
        .prefetch_related("authors", "genres")
    )

    scored = []
    for book in candidates:
        score = 0.0
        for g in book.genres.all():
            score += genre_weight.get(g.id, 0) * 3
        for a in book.authors.all():
            score += author_weight.get(a.id, 0) * 4
        score += book.avg_rating * 0.5
        scored.append((score, book))

    scored.sort(key=lambda x: -x[0])
    result = [b for _, b in scored[:limit]]

    if len(result) < limit:
        seen_ids = seen_ids | {b.pk for b in result}
        extra = list(
            Book.objects.exclude(pk__in=seen_ids)
            .prefetch_related("authors", "genres")
            .order_by("-avg_rating")[: limit - len(result)]
        )
        result += extra

    return result


def _cold_start(user, limit):
    """Холодный старт: используем предпочтения из онбординга, иначе популярное."""

    profile = getattr(user, "profile", None)
    fav_genres = list(profile.favorite_genres.values_list("id", flat=True)) if profile else []
    fav_authors = list(profile.favorite_authors.values_list("id", flat=True)) if profile else []

    if fav_genres or fav_authors:
        q = Q()
        if fav_genres:
            q |= Q(genres__id__in=fav_genres)
        if fav_authors:
            q |= Q(authors__id__in=fav_authors)
        return list(
            Book.objects.filter(q).distinct()
            .prefetch_related("authors", "genres")
            .order_by("-avg_rating")[:limit]
        )

    return list(
        Book.objects
        .prefetch_related("authors", "genres")
        .order_by("-avg_rating")[:limit]
    )
