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
  применяем TF-IDF для редких жанров, temporal decay (свежее = важнее),
  штрафуем книги из отрицательных списков,
  MMR-диверсификация: один автор / жанр не заполняет весь топ.
  находим непрочитанные книги с максимальным попаданием.

Коллаборативная фильтрация (also_read):
  Если пользователи A, B, C добавили книгу X и книгу Y в один список —
  значит Y похожа на X. Чистый SQL, без ML.
"""

import math
from django.core.cache import cache
from django.db.models import Q, Count
from django.utils import timezone

from .models import Book, UserList, Genre, ReadingProgress
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

    # ── 1. Сбор данных из списков пользователя ─────────────────────────────
    user_lists = (
        UserList.objects.filter(user=user)
        .prefetch_related("books__genres", "books__authors")
    )

    user_reviews = Review.objects.filter(user=user).values("book_id", "rating")
    reviewed = {r["book_id"]: r["rating"] for r in user_reviews}
    seen_ids = set(reviewed.keys())

    # Исключаем книги, которые пользователь сейчас читает
    reading_ids = ReadingProgress.objects.filter(
        user=user, current_page__gt=0
    ).values_list("book_id", flat=True)
    seen_ids.update(reading_ids)

    genre_weight = {}
    author_weight = {}
    negative_genres = set()    # жанры из отрицательных списков
    negative_authors = set()   # авторы из отрицательных списков

    now = timezone.now()

    # ── 2. Взвешиваем: sentiment × rating × temporal decay ─────────────────
    for ul in user_lists:
        sw = _SENTIMENT_WEIGHT.get(ul.sentiment_tag or "neutral", 0.4)

        # Temporal decay: 5 % затухание в месяц — свежие списки важнее
        list_age_days = max((now - ul.created_at).days, 0)
        decay = 0.95 ** (list_age_days / 30)

        for book in ul.books.all():
            seen_ids.add(book.pk)
            rating_factor = reviewed.get(book.pk, 5) / 5.0
            weight = sw * rating_factor * decay

            for g in book.genres.all():
                genre_weight[g.id] = genre_weight.get(g.id, 0) + weight
                if ul.sentiment_tag == "negative":
                    negative_genres.add(g.id)
            for a in book.authors.all():
                author_weight[a.id] = author_weight.get(a.id, 0) + weight
                if ul.sentiment_tag == "negative":
                    negative_authors.add(a.id)

    if not genre_weight and not author_weight and not reviewed:
        return _cold_start(user, limit)

    # Учитываем отзывы на книги, которых нет в списках
    orphan_review_ids = set(reviewed.keys()) - seen_ids
    if orphan_review_ids:
        for b in Book.objects.filter(pk__in=orphan_review_ids).prefetch_related("genres", "authors"):
            seen_ids.add(b.pk)
            w = 0.4 * (reviewed[b.pk] / 5.0)
            for g in b.genres.all():
                genre_weight[g.id] = genre_weight.get(g.id, 0) + w
            for a in b.authors.all():
                author_weight[a.id] = author_weight.get(a.id, 0) + w

    # Подписки на авторов — сильный положительный сигнал
    sub_author_ids = user.author_subscriptions.values_list("author_id", flat=True)
    for author_id in sub_author_ids:
        author_weight[author_id] = author_weight.get(author_id, 0) + 1.5

    # TF-IDF: редкий жанр ценнее
    idf = _genre_idf()
    for gid in genre_weight:
        genre_weight[gid] *= idf.get(gid, 1.0)

    if not genre_weight and not author_weight:
        return _cold_start(user, limit)

    # ── 3. Скоринг кандидатов ──────────────────────────────────────────────
    candidates = (
        Book.objects
        .exclude(pk__in=seen_ids)
        .filter(
            Q(genres__id__in=genre_weight.keys()) |
            Q(authors__id__in=author_weight.keys())
        )
        .distinct()
        .prefetch_related("authors", "genres", "tags")
    )

    scored = []
    for book in candidates:
        score = 0.0
        book_genres = {g.id for g in book.genres.all()}
        book_authors = {a.id for a in book.authors.all()}

        for gid in book_genres:
            score += genre_weight.get(gid, 0) * 3
        for aid in book_authors:
            score += author_weight.get(aid, 0) * 4

        # Штраф за пересечение с отрицательными списками
        neg_overlap = len(book_genres & negative_genres) * 2.0
        neg_overlap += len(book_authors & negative_authors) * 3.0
        score -= neg_overlap

        score += book.avg_rating * 0.5

        if score > 0:
            scored.append((score, book))

    # ── 4. MMR-диверсификация: один автор/жанр не заполняет весь топ ───────
    result = [b for _, b in _mmr_rerank(scored, limit)]

    if len(result) < limit:
        seen_ids = seen_ids | {b.pk for b in result}
        extra = list(
            Book.objects.exclude(pk__in=seen_ids)
            .prefetch_related("authors", "genres")
            .order_by("-avg_rating")[: limit - len(result)]
        )
        result += extra

    return result


def _mmr_rerank(scored, limit, lam=0.6):
    """
    Maximal Marginal Relevance — жадный отбор с штрафом
    за похожесть на уже отобранные книги.
    lam=1 → чистая релевантность, lam=0 → максимальное разнообразие.
    """
    if not scored:
        return []
    scored.sort(key=lambda x: -x[0])
    if len(scored) <= limit:
        return scored

    selected = [scored[0]]
    remaining = scored[1:]

    while len(selected) < limit and remaining:
        best_idx, best_mmr = 0, -999.0
        for i, (score, book) in enumerate(remaining):
            b_authors = {a.id for a in book.authors.all()}
            b_genres = {g.id for g in book.genres.all()}

            max_sim = 0.0
            for _, sel in selected:
                s_a = {a.id for a in sel.authors.all()}
                s_g = {g.id for g in sel.genres.all()}
                a_sim = len(b_authors & s_a) / max(len(b_authors | s_a), 1)
                g_sim = len(b_genres & s_g) / max(len(b_genres | s_g), 1)
                max_sim = max(max_sim, 0.5 * a_sim + 0.5 * g_sim)

            mmr = lam * score - (1 - lam) * max_sim * scored[0][0]
            if mmr > best_mmr:
                best_mmr, best_idx = mmr, i

        selected.append(remaining.pop(best_idx))

    return selected


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
