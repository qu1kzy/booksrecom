from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import HttpResponse, JsonResponse, Http404, HttpResponseBadRequest
from django.core.paginator import Paginator
from django.conf import settings
from django.db.models import Q, Min, Max, Avg, Count, Exists, OuterRef
from django.db.models.functions import TruncDate
from django.views.decorators.http import require_GET, require_POST, require_http_methods
from django.core.exceptions import PermissionDenied

import requests as http_req
from celery.result import AsyncResult

from .models import (
    Book, Genre, UserList, Store, BookStore, Language, Author,
    BookPrice, ReadingProgress, Quote, PriceAlert, Publisher, Series,
    MoodTag, BookMood,
)
from reviews.models import Review, ReviewLike
from .recommendations import similar_books as get_similar, also_read as get_also_read
from users.models import AuthorSubscription
from .ai_recommendations import invalidate as invalidate_ai_cache
from .tasks import scrape_book_prices, generate_smart_quotes
from .isbn_lookup import lookup_by_isbn
from .reading_pace import predict_reading_time

# ─── ДЕКОРАТОРЫ ───────────────────────────────────────────────────────────────

def staff_required(view_func):
    """Декоратор, разрешающий доступ только персоналу (staff)."""
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_staff:
            raise PermissionDenied
        return view_func(request, *args, **kwargs)
    return _wrapped

# ─── ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ─────────────────────────────────────────────────

def _filter_books(params, base_qs=None):
    """
    Применяет фильтры из GET-параметров к queryset книг.
    Возвращает (qs, filter_context), где filter_context — словарь с
    выбранными значениями фильтров для передачи в шаблон.
    """
    if base_qs is None:
        qs = Book.objects.prefetch_related("authors", "genres").select_related("publisher", "language")
    else:
        qs = base_qs

    # Текстовый поиск
    search = params.get("search", "").strip()
    if search:
        qs = qs.filter(
            Q(title__icontains=search)
            | Q(authors__name__icontains=search)
            | Q(genres__name__icontains=search)
            | Q(description__icontains=search)
            | Q(isbn__iexact=search)
        ).distinct()

    # Мультиселекты
    genre_ids = params.getlist("genre")
    for gid in genre_ids:
        qs = qs.filter(genres__id=gid)
    if genre_ids:
        qs = qs.distinct()

    author_ids = params.getlist("author")
    for aid in author_ids:
        qs = qs.filter(authors__id=aid)
    if author_ids:
        qs = qs.distinct()

    language_ids = params.getlist("language")
    if language_ids:
        qs = qs.filter(language__id__in=language_ids)

    # Диапазоны
    year_from = params.get("year_from", "").strip()
    year_to   = params.get("year_to", "").strip()
    if year_from.isdigit():
        qs = qs.filter(publication_year__gte=int(year_from))
    if year_to.isdigit():
        qs = qs.filter(publication_year__lte=int(year_to))

    pages_from = params.get("pages_from", "").strip()
    pages_to   = params.get("pages_to", "").strip()
    if pages_from.isdigit():
        qs = qs.filter(pages__gte=int(pages_from))
    if pages_to.isdigit():
        qs = qs.filter(pages__lte=int(pages_to))

    price_from = params.get("price_from", "").strip()
    price_to   = params.get("price_to", "").strip()
    if price_from:
        try:
            qs = qs.filter(avg_price__gte=float(price_from))
        except ValueError:
            pass
    if price_to:
        try:
            qs = qs.filter(avg_price__lte=float(price_to))
        except ValueError:
            pass

    rating_min = params.get("rating_min", "").strip()
    if rating_min:
        try:
            qs = qs.filter(avg_rating__gte=float(rating_min))
        except ValueError:
            pass

    # Mood-фильтр
    mood_ids = params.getlist("mood")
    if mood_ids:
        qs = qs.filter(moods__mood_id__in=mood_ids).distinct()

    # Сортировка
    ordering = params.get("ordering", "-avg_rating")
    if ordering in {"-avg_rating", "-rating_count", "-publication_year",
                    "publication_year", "avg_price", "-avg_price"}:
        qs = qs.order_by(ordering)

    # Контекст для шаблона (выбранные значения)
    filter_ctx = {
        "search": search,
        "selected_genres": genre_ids,
        "selected_authors": author_ids,
        "selected_languages": language_ids,
        "selected_moods": mood_ids,
        "year_from": year_from,
        "year_to": year_to,
        "pages_from": pages_from,
        "pages_to": pages_to,
        "price_from": price_from,
        "price_to": price_to,
        "rating_min": rating_min,
        "ordering": ordering,
    }
    return qs, filter_ctx

def _get_book_detail_context(book, request):
    # Одобренные рецензии — аннотированы лайками, отсортированы по популярности
    _like_filter = (
        ReviewLike.objects.filter(review=OuterRef("pk"), user=request.user)
        if request.user.is_authenticated
        else ReviewLike.objects.none()
    )
    REVIEWS_PER_PAGE = 5
    reviews_qs = (
        Review.objects
        .filter(book=book, status=Review.APPROVED)
        .select_related("user")
        .annotate(
            likes_count=Count("likes", distinct=True),
            user_liked=Exists(_like_filter),
        )
        .order_by("-likes_count", "-created_at")
    )
    review_count = reviews_qs.count()
    reviews = reviews_qs[:REVIEWS_PER_PAGE]
    has_more_reviews = review_count > REVIEWS_PER_PAGE
    user_has_review = reviews_qs.filter(user=request.user).exists() if request.user.is_authenticated else False

    # Списки пользователя
    user_lists = []
    book_list_ids = set()
    if request.user.is_authenticated:
        user_lists = UserList.objects.filter(user=request.user)
        book_list_ids = set(user_lists.filter(books=book).values_list("id", flat=True))

    # Ссылки на магазины
    store_links = list(book.store_links.select_related("store").filter(store__is_active=True))
    linked_ids = {sl.store_id for sl in store_links}
    unlinked_stores = [s for s in Store.objects.filter(is_active=True) if s.id not in linked_ids]

    # Данные для инлайн-редактирования (только staff)
    edit_author_ids = "[" + ",".join(str(a.pk) for a in book.authors.all()) + "]"
    edit_genre_ids  = "[" + ",".join(str(g.pk) for g in book.genres.all()) + "]"

    # Прогресс чтения и алерт цены текущего пользователя
    reading_progress = None
    user_price_alert = None
    reading_prediction = None
    if request.user.is_authenticated:
        reading_progress = ReadingProgress.objects.filter(user=request.user, book=book).first()
        user_price_alert = PriceAlert.objects.filter(user=request.user, book=book).first()
        reading_prediction = predict_reading_time(request.user, book)

    return {
        "book": book,
        "reviews": reviews,
        "review_count": review_count,
        "has_more_reviews": has_more_reviews,
        "next_page": 2,
        "user_lists": user_lists,
        "book_list_ids": book_list_ids,
        "store_links": store_links,
        "unlinked_stores": unlinked_stores,
        "similar": get_similar(book, limit=5),
        "also_read": get_also_read(book, limit=6),
        "user_has_review": user_has_review,
        "active_tab": request.GET.get("tab", "about"),
        "quotes": Quote.objects.filter(book=book).select_related("user", "mood_tag"),
        "quotes_count": Quote.objects.filter(book=book).count(),
        "moods": BookMood.objects.filter(book=book).select_related("mood").order_by("-confidence", "-vote_count"),
        "reading_progress": reading_progress,
        "reading_prediction": reading_prediction,
        "user_price_alert": user_price_alert,
        "all_authors": Author.objects.order_by("name"),
        "all_genres": Genre.objects.order_by("name"),
        "all_languages": Language.objects.order_by("name"),
        "all_publishers": Publisher.objects.order_by("name"),
        "all_series": Series.objects.order_by("name"),
        "edit_author_ids": edit_author_ids,
        "edit_genre_ids": edit_genre_ids,
        "edit_publisher_id": book.publisher_id,
        "edit_publisher_name": book.publisher.name if book.publisher else "",
        "edit_series_id": book.series_id,
        "edit_series_name": book.series.name if book.series else "",
    }

def _get_author_detail_context(author, request):
    """Собирает контекст для страницы автора."""
    params = request.GET
    base_qs = author.books.prefetch_related("authors", "genres").select_related("publisher", "language")
    qs, filter_ctx = _filter_books(params, base_qs)

    paginator = Paginator(qs, settings.BOOKS_PER_PAGE)
    page = paginator.get_page(params.get("page", 1))

    # Убираем page из query_string для ссылок пагинации
    query_dict = params.copy()
    query_dict.pop("page", None)
    query_string = query_dict.urlencode()

    # Агрегаты для слайдеров
    agg = author.books.aggregate(
        min_year=Min("publication_year"),
        max_year=Max("publication_year"),
    )

    # Подписка
    is_subscribed = False
    if request.user.is_authenticated:
        is_subscribed = AuthorSubscription.objects.filter(user=request.user, author=author).exists()

    # Все жанры, в которых есть книги этого автора
    all_genres = Genre.objects.filter(books__authors=author).distinct()

    has_filters = any([
        filter_ctx["search"], filter_ctx["selected_genres"],
        filter_ctx["year_from"], filter_ctx["year_to"],
        filter_ctx["rating_min"]
    ])

    return {
        "author": author,
        "books": page,
        "total": paginator.count,
        "query_string": query_string,
        "has_filters": has_filters,
        "all_genres": all_genres,
        "selected_genres": filter_ctx["selected_genres"],
        "agg": agg,
        "f": params,
        "is_subscribed": is_subscribed,
    }

def _inline_create(request, model_class, name_field="name"):
    """
    Общая функция для создания объекта через AJAX.
    Принимает POST-запрос с полем name_field, возвращает JSON.
    """
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)
    name = request.POST.get(name_field, "").strip()
    if not name:
        return JsonResponse({"error": f"Поле {name_field} обязательно"}, status=400)
    obj, created = model_class.objects.get_or_create(**{name_field: name})
    return JsonResponse({"id": obj.pk, "name": getattr(obj, name_field), "created": created})

# ─── КАТАЛОГ ─────────────────────────────────────────────────────────────────

@require_GET
def catalog(request):
    params = request.GET
    qs, filter_ctx = _filter_books(params)

    # Пагинация
    paginator = Paginator(qs, settings.BOOKS_PER_PAGE)
    page = paginator.get_page(params.get("page", 1))

    # Убираем page из query_string
    query_dict = params.copy()
    query_dict.pop("page", None)
    query_string = query_dict.urlencode()

    # Агрегаты для диапазонов (глобальные минимумы/максимумы)
    agg = Book.objects.aggregate(
        min_year=Min("publication_year"), max_year=Max("publication_year"),
        min_pages=Min("pages"), max_pages=Max("pages"),
        min_price=Min("avg_price"), max_price=Max("avg_price"),
    )

    has_filters = any([
        filter_ctx["search"], filter_ctx["selected_genres"],
        filter_ctx["selected_authors"], filter_ctx["selected_languages"],
        filter_ctx["selected_moods"],
        filter_ctx["year_from"], filter_ctx["year_to"],
        filter_ctx["pages_from"], filter_ctx["pages_to"],
        filter_ctx["price_from"], filter_ctx["price_to"],
        filter_ctx["rating_min"]
    ])

    ctx = {
        "books": page,
        "total": paginator.count,
        "query_string": query_string,
        "has_filters": has_filters,
        "all_genres": Genre.objects.all(),
        "all_authors": Author.objects.all()[:200],
        "all_languages": Language.objects.all(),
        "all_moods": MoodTag.objects.all(),
        "selected_genres": filter_ctx["selected_genres"],
        "selected_authors": filter_ctx["selected_authors"],
        "selected_languages": filter_ctx["selected_languages"],
        "selected_moods": filter_ctx["selected_moods"],
        "agg": agg,
        "f": params,
    }
    if getattr(request, "htmx", False):
        return render(request, "books/_book_list.html", ctx)
    return render(request, "books/catalog.html", ctx)

# ─── СТРАНИЦА КНИГИ ──────────────────────────────────────────────────────────

@require_GET
def book_detail(request, pk):
    book = get_object_or_404(
        Book.objects.prefetch_related("authors", "genres", "store_links__store"),
        pk=pk
    )
    ctx = _get_book_detail_context(book, request)

    # Запуск генерации AI-цитат, если их ещё нет
    if not book.quotes.filter(is_ai_generated=True).exists():
        generate_smart_quotes.delay(book.pk)

    return render(request, "books/book_detail.html", ctx)

# ─── УПРАВЛЕНИЕ СПИСКАМИ ─────────────────────────────────────────────────────

@login_required
@require_POST
def toggle_list(request):
    book = get_object_or_404(Book, pk=request.POST.get("book_id"))
    list_id = request.POST.get("list_id")
    user_list = get_object_or_404(UserList, pk=list_id, user=request.user)

    if user_list.books.filter(pk=book.pk).exists():
        user_list.books.remove(book)
    else:
        user_list.books.add(book)
        # Событие для ленты
        from social.models import ActivityEvent
        ActivityEvent.objects.create(
            user=request.user,
            event_type="add_to_list",
            book=book,
            metadata={"list_name": user_list.name},
        )

    # Инвалидация AI‑кеша
    invalidate_ai_cache(request.user.pk)

    # Актуальные списки пользователя
    book_list_ids = set(
        UserList.objects.filter(user=request.user, books=book).values_list("id", flat=True)
    )
    user_lists = UserList.objects.filter(user=request.user)

    return render(request, "books/_list_dropdown.html", {
        "book": book,
        "user_lists": user_lists,
        "book_list_ids": book_list_ids,
        "partial": True
    })

# ─── ЗАПРОС ЦЕНЫ + ПОЛЛИНГ ───────────────────────────────────────────────────

@login_required
@require_POST
def request_price(request, pk):
    book = get_object_or_404(Book, pk=pk)

    # reCAPTCHA v2 verification
    recaptcha_secret = getattr(settings, "RECAPTCHA_PRIVATE_KEY", "")
    if recaptcha_secret:
        token = request.POST.get("g-recaptcha-response", "")
        if not token:
            return render(request, "books/_price_block.html", {
                "book": book, "pending": False, "captcha_error": True
            })
        resp = http_req.post(
            "https://www.google.com/recaptcha/api/siteverify",
            data={"secret": recaptcha_secret, "response": token},
            timeout=10,
        )
        result = resp.json()
        if not result.get("success"):
            return render(request, "books/_price_block.html", {
                "book": book, "pending": False, "captcha_error": True
            })

    result = scrape_book_prices.delay(book.pk)
    request.session[f"price_task_{book.pk}"] = result.id

    return render(request, "books/_price_block.html", {
        "book": book, "pending": True, "task_id": result.id
    })

@login_required
@require_GET
def price_captcha(request, pk):
    book = get_object_or_404(Book, pk=pk)
    return render(request, "books/_price_captcha.html", {
        "book": book,
        "recaptcha_site_key": settings.RECAPTCHA_PUBLIC_KEY,
    })

@require_GET
def price_status(request, pk):
    book = get_object_or_404(Book, pk=pk)
    task_id = request.GET.get("task_id") or request.session.get(f"price_task_{book.pk}")

    done = True
    if task_id:
        result = AsyncResult(task_id)
        done = result.ready()

    if done:
        book.refresh_from_db()
        return render(request, "books/_price_block.html", {"book": book, "pending": False})
    return render(request, "books/_price_block.html", {
        "book": book, "pending": True, "task_id": task_id
    })

@require_GET
def price_chart_data(request, pk):
    book = get_object_or_404(Book, pk=pk)
    store_links = BookStore.objects.filter(book=book).select_related("store")

    datasets = []
    all_dates = set()
    palette = ["#6366f1", "#f59e0b", "#10b981", "#ef4444", "#8b5cf6", "#06b6d4"]

    for i, link in enumerate(store_links):
        rows = (
            BookPrice.objects
            .filter(book_store=link)
            .annotate(day=TruncDate("created_at"))
            .values("day")
            .annotate(avg=Avg("price"))
            .order_by("day")
        )
        if not rows:
            continue

        data = {str(r["day"]): float(r["avg"]) for r in rows}
        all_dates.update(data.keys())

        datasets.append({
            "label": link.store.name,
            "data": data,
            "color": palette[i % len(palette)],
            "borderDash": [],
        })

    if not all_dates:
        return JsonResponse({"labels": [], "datasets": []})

    labels = sorted(all_dates)

    # Средняя по всем магазинам за день
    avg_rows = (
        BookPrice.objects
        .filter(book_store__book=book)
        .annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(avg=Avg("price"))
        .order_by("day")
    )
    avg_data = {str(r["day"]): float(r["avg"]) for r in avg_rows}

    datasets.append({
        "label": "Средняя",
        "data": avg_data,
        "color": "#111111",
        "borderDash": [6, 3],
    })

    # Нормализуем: для каждого датасета список значений по labels
    for ds in datasets:
        ds["points"] = [ds["data"].get(l) for l in labels]
        del ds["data"]

    return JsonResponse({"labels": labels, "datasets": datasets})

# ─── СТРАНИЦА АВТОРА ─────────────────────────────────────────────────────────

@require_GET
def author_detail(request, pk):
    author = get_object_or_404(Author.objects.prefetch_related("books"), pk=pk)
    ctx = _get_author_detail_context(author, request)
    if getattr(request, "htmx", False):
        return render(request, "books/_book_list.html", ctx)
    return render(request, "books/author_detail.html", ctx)

# ─── УПРАВЛЕНИЕ ССЫЛКАМИ НА МАГАЗИНЫ (STAFF) ─────────────────────────────────

@staff_required
@require_POST
def store_link_save(request, book_id):
    book = get_object_or_404(Book, pk=book_id)
    store = get_object_or_404(Store, pk=request.POST.get("store_id"))
    url = request.POST.get("product_url", "").strip()
    if not url:
        return HttpResponseBadRequest("URL обязателен")
    BookStore.objects.update_or_create(
        book=book, store=store,
        defaults={"product_url": url},
    )
    return _render_store_links(request, book)

@staff_required
@require_POST
def store_link_delete(request, book_id, store_id):
    BookStore.objects.filter(book_id=book_id, store_id=store_id).delete()
    book = get_object_or_404(Book, pk=book_id)
    return _render_store_links(request, book)

def _render_store_links(request, book):
    """Рендерит частичный шаблон со ссылками на магазины."""
    store_links = list(book.store_links.select_related("store").filter(store__is_active=True))
    linked_ids = {sl.store_id for sl in store_links}
    unlinked_stores = [s for s in Store.objects.filter(is_active=True) if s.id not in linked_ids]
    return render(request, "books/_store_links.html", {
        "book": book,
        "store_links": store_links,
        "unlinked_stores": unlinked_stores,
    })

# ─── ADMIN PARTIALS ───────────────────────────────────────────────────────────

@staff_required
@require_POST
def admin_delete_book(request, pk):
    get_object_or_404(Book, pk=pk).delete()
    return HttpResponse("")

@staff_required
@require_GET
def admin_books_partial(request):
    q = request.GET.get("q", "")
    qs = Book.objects.prefetch_related("authors", "genres")
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(authors__name__icontains=q)).distinct()
    return render(request, "books/_admin_books.html", {"books": qs[:50]})

# ─── ISBN LOOKUP (STAFF) ─────────────────────────────────────────────────────

@staff_required
@require_GET
def isbn_lookup(request):
    """HTMX endpoint: ищет книгу по ISBN через Google Books / Open Library."""
    isbn = request.GET.get("isbn", "").strip()
    if len(isbn) < 10:
        return HttpResponse("")
    data = lookup_by_isbn(isbn)
    if not data:
        return render(request, "books/_isbn_preview.html", {"not_found": True, "isbn": isbn})

    # Матчим авторов из API с авторами в БД
    author_matches = []  # [{api_name, db_author, exact}]
    all_authors_qs = Author.objects.order_by("name")
    for api_name in (data.get("authors") or []):
        api_lower = api_name.lower().strip()
        exact = Author.objects.filter(name__iexact=api_name.strip()).first()
        if exact:
            author_matches.append({"api_name": api_name, "db_author": exact, "exact": True, "candidates": []})
        else:
            # Частичное совпадение — по словам из имени
            words = [w for w in api_lower.split() if len(w) > 2]
            from django.db.models import Q as _Q
            q = _Q()
            for w in words:
                q |= _Q(name__icontains=w)
            candidates = list(Author.objects.filter(q).order_by("name")[:10]) if words else []
            author_matches.append({"api_name": api_name, "db_author": None, "exact": False, "candidates": candidates})

    # Матчим жанры из API с жанрами в БД
    genre_matches = []
    for api_genre in (data.get("genres") or []):
        exact = Genre.objects.filter(name__iexact=api_genre.strip()).first()
        if exact:
            genre_matches.append({"api_name": api_genre, "db_genre": exact, "exact": True, "candidates": []})
        else:
            candidates = list(Genre.objects.filter(name__icontains=api_genre.strip()[:20]).order_by("name")[:10])
            genre_matches.append({"api_name": api_genre, "db_genre": None, "exact": False, "candidates": candidates})

    ctx = {
        "data": data,
        "author_matches": author_matches,
        "genre_matches": genre_matches,
        "all_authors": all_authors_qs,
        "all_genres": Genre.objects.order_by("name"),
    }
    return render(request, "books/_isbn_preview.html", ctx)

# ─── ДОБАВЛЕНИЕ / РЕДАКТИРОВАНИЕ КНИГ (STAFF) ────────────────────────────────

@staff_required
@require_http_methods(["GET", "POST"])
def book_add(request):
    copy_from = None
    form_data = {}
    selected_author_ids = "[]"
    selected_genre_ids = "[]"
    selected_publisher_id = None
    selected_series_id = None

    copy_pk = request.GET.get("copy_from") or request.POST.get("copy_from")
    if copy_pk:
        try:
            copy_from = Book.objects.prefetch_related("authors", "genres").get(pk=copy_pk)
            form_data = {
                "title": copy_from.title + " (копия)",
                "isbn": "",
                "description": copy_from.description,
                "publication_year": copy_from.publication_year,
                "pages": copy_from.pages,
                "language_id": copy_from.language_id,
                "publisher_name": copy_from.publisher.name if copy_from.publisher else "",
                "series_name": copy_from.series.name if copy_from.series else "",
            }
            selected_author_ids = "[" + ",".join(str(a.pk) for a in copy_from.authors.all()) + "]"
            selected_genre_ids = "[" + ",".join(str(g.pk) for g in copy_from.genres.all()) + "]"
            selected_publisher_id = copy_from.publisher_id
            selected_series_id = copy_from.series_id
        except Book.DoesNotExist:
            pass

    if request.method == "POST":
        title = request.POST.get("title", "").strip()
        if not title:
            ctx = _book_form_context()
            ctx.update({
                "error": "Название обязательно",
                "form_data": request.POST,
                "copy_from": copy_from,
                "selected_author_ids": "[" + ",".join(request.POST.getlist("authors")) + "]",
                "selected_genre_ids": "[" + ",".join(request.POST.getlist("genres")) + "]",
                "selected_publisher_id": request.POST.get("publisher_id") or None,
                "selected_series_id": request.POST.get("series_id") or None,
            })
            return render(request, "books/book_add.html", ctx)

        # Издательство и серия
        publisher = _get_or_create_publisher(request)
        series = _get_or_create_series(request)
        language_pk = request.POST.get("language", "").strip()
        language = Language.objects.filter(pk=language_pk).first() if language_pk else None

        pub_year = request.POST.get("publication_year", "").strip()
        pages = request.POST.get("pages", "").strip()

        book = Book.objects.create(
            title=title,
            isbn=request.POST.get("isbn", "").strip() or None,
            description=request.POST.get("description", "").strip(),
            publication_year=int(pub_year) if pub_year.isdigit() else None,
            pages=int(pages) if pages.isdigit() else None,
            publisher=publisher,
            series=series,
            language=language,
        )

        if "cover_image" in request.FILES:
            book.cover_image = request.FILES["cover_image"]
            book.save(update_fields=["cover_image"])

        author_ids = request.POST.getlist("authors")
        genre_ids = request.POST.getlist("genres")
        if author_ids:
            book.authors.set(Author.objects.filter(pk__in=author_ids))
        if genre_ids:
            book.genres.set(Genre.objects.filter(pk__in=genre_ids))

        messages.success(request, f"Книга «{book.title}» добавлена.")
        return redirect("book_detail", pk=book.pk)

    ctx = _book_form_context()
    ctx.update({
        "copy_from": copy_from,
        "form_data": form_data,
        "selected_author_ids": selected_author_ids,
        "selected_genre_ids": selected_genre_ids,
        "selected_publisher_id": selected_publisher_id,
        "selected_series_id": selected_series_id,
    })
    return render(request, "books/book_add.html", ctx)

@staff_required
@require_POST
def book_edit(request, pk):
    book = get_object_or_404(Book, pk=pk)

    title = request.POST.get("title", "").strip()
    if not title:
        messages.error(request, "Название не может быть пустым.")
        return redirect("book_detail", pk=pk)

    publisher = _get_or_create_publisher(request)
    series = _get_or_create_series(request)
    language_pk = request.POST.get("language", "").strip()
    language = Language.objects.filter(pk=language_pk).first() if language_pk else None

    pub_year = request.POST.get("publication_year", "").strip()
    pages = request.POST.get("pages", "").strip()

    book.title = title
    book.isbn = request.POST.get("isbn", "").strip() or None
    book.description = request.POST.get("description", "").strip()
    book.publication_year = int(pub_year) if pub_year.isdigit() else None
    book.pages = int(pages) if pages.isdigit() else None
    book.publisher = publisher
    book.series = series
    book.language = language
    book.save()

    if "cover_image" in request.FILES:
        book.cover_image = request.FILES["cover_image"]
        book.save(update_fields=["cover_image"])

    author_ids = request.POST.getlist("authors")
    genre_ids = request.POST.getlist("genres")
    book.authors.set(Author.objects.filter(pk__in=author_ids))
    book.genres.set(Genre.objects.filter(pk__in=genre_ids))

    messages.success(request, f"Книга «{book.title}» обновлена.")
    return redirect("book_detail", pk=pk)

def _book_form_context():
    """Общий контекст для формы добавления/редактирования книги."""
    return {
        "all_genres": Genre.objects.order_by("name"),
        "all_authors": Author.objects.order_by("name"),
        "all_languages": Language.objects.order_by("name"),
        "all_publishers": Publisher.objects.order_by("name"),
        "all_series": Series.objects.order_by("name"),
    }

def _get_or_create_publisher(request):
    """Извлекает или создаёт издательство из POST-данных."""
    pub_id = request.POST.get("publisher_id", "").strip()
    pub_name = request.POST.get("publisher_name", "").strip()
    publisher = None
    if pub_id and pub_id.isdigit():
        publisher = Publisher.objects.filter(pk=pub_id).first()
    if not publisher and pub_name:
        publisher, _ = Publisher.objects.get_or_create(name=pub_name)
    return publisher

def _get_or_create_series(request):
    """Извлекает или создаёт серию из POST-данных."""
    ser_id = request.POST.get("series_id", "").strip()
    ser_name = request.POST.get("series_name", "").strip()
    series = None
    if ser_id and ser_id.isdigit():
        series = Series.objects.filter(pk=ser_id).first()
    if not series and ser_name:
        series, _ = Series.objects.get_or_create(name=ser_name)
    return series

# ─── INLINE-СОЗДАНИЕ ОБЪЕКТОВ (STAFF) ────────────────────────────────────────

@staff_required
def author_create_inline(request):
    return _inline_create(request, Author)

@staff_required
def genre_create_inline(request):
    return _inline_create(request, Genre)

@staff_required
def publisher_create_inline(request):
    return _inline_create(request, Publisher)

@staff_required
def series_create_inline(request):
    return _inline_create(request, Series)

# ─── ПРОГРЕСС ЧТЕНИЯ ─────────────────────────────────────────────────────────

@login_required
@require_POST
def reading_progress_save(request, pk):
    book = get_object_or_404(Book, pk=pk)
    page = request.POST.get("current_page", "0").strip()
    if not page.isdigit():
        return HttpResponseBadRequest("Страница должна быть числом")
    page = min(int(page), book.pages or 999999)
    progress, _ = ReadingProgress.objects.update_or_create(
        user=request.user, book=book,
        defaults={"current_page": page},
    )
    return JsonResponse({"current_page": progress.current_page, "percent": progress.percent()})

# ─── ЦИТАТЫ ───────────────────────────────────────────────────────────────────

@login_required
@require_POST
def quote_add(request, pk):
    book = get_object_or_404(Book, pk=pk)
    text = request.POST.get("text", "").strip()
    if not text:
        return HttpResponseBadRequest("Текст цитаты обязателен")
    page_raw = request.POST.get("page_number", "").strip()
    page = int(page_raw) if page_raw.isdigit() else None
    Quote.objects.create(user=request.user, book=book, text=text, page_number=page)
    quotes = Quote.objects.filter(book=book).select_related("user")
    return render(request, "books/_quotes.html", {"book": book, "quotes": quotes})

@login_required
@require_POST
def quote_delete(request, pk, quote_pk):
    book = get_object_or_404(Book, pk=pk)
    get_object_or_404(Quote, pk=quote_pk, user=request.user).delete()
    quotes = Quote.objects.filter(book=book).select_related("user")
    return render(request, "books/_quotes.html", {"book": book, "quotes": quotes})

@require_GET
def quotes_partial(request, pk):
    book = get_object_or_404(Book, pk=pk)
    quotes = Quote.objects.filter(book=book).select_related("user")
    return render(request, "books/_quotes.html", {"book": book, "quotes": quotes})

# ─── АЛЕРТ ЦЕНЫ ───────────────────────────────────────────────────────────────

@login_required
@require_POST
def price_alert_save(request, pk):
    book = get_object_or_404(Book, pk=pk)
    threshold = request.POST.get("threshold", "").strip().replace(",", ".")
    try:
        threshold = float(threshold)
    except ValueError:
        return HttpResponseBadRequest("Некорректное значение порога")
    PriceAlert.objects.update_or_create(
        user=request.user, book=book,
        defaults={"threshold": threshold, "triggered_at": None},
    )
    alert = PriceAlert.objects.get(user=request.user, book=book)
    return render(request, "books/_price_alert.html", {"book": book, "alert": alert})

@login_required
@require_POST
def price_alert_delete(request, pk):
    book = get_object_or_404(Book, pk=pk)
    PriceAlert.objects.filter(user=request.user, book=book).delete()
    return render(request, "books/_price_alert.html", {"book": book, "alert": None})

# ─── MOOD TAGS ───────────────────────────────────────────────────────────────

@login_required
@require_POST
def vote_mood(request, pk, mood_id):
    """Голосование за mood-тег книги. HTMX partial."""
    book = get_object_or_404(Book, pk=pk)
    mood_tag = get_object_or_404(MoodTag, pk=mood_id)
    bm, created = BookMood.objects.get_or_create(
        book=book, mood=mood_tag,
        defaults={"source": "user_vote", "confidence": 0.7, "vote_count": 1},
    )
    if not created:
        bm.vote_count += 1
        bm.save(update_fields=["vote_count"])
    moods = BookMood.objects.filter(book=book).select_related("mood").order_by("-confidence", "-vote_count")
    return render(request, "books/_mood_tags.html", {"book": book, "moods": moods})