from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib import messages
from django.http import HttpResponse, JsonResponse
from django.core.paginator import Paginator
from django.conf import settings
from django.db.models import Q, Min, Max
from django.utils import timezone

from .models import Book, Genre, UserList, Store, BookStore, Language, Author


# ── Каталог ───────────────────────────────────────────────────────────────────

def catalog(request):
    qs = Book.objects.prefetch_related("authors", "genres").select_related("publisher", "language")

    g = request.GET

    # Текстовый поиск
    search = g.get("search", "").strip()
    if search:
        qs = qs.filter(
            Q(title__icontains=search)
            | Q(authors__name__icontains=search)
            | Q(genres__name__icontains=search)
            | Q(description__icontains=search)
            | Q(isbn__iexact=search)
        ).distinct()

    # Мультиселект: жанры
    genre_ids = g.getlist("genre")
    if genre_ids:
        for gid in genre_ids:
            qs = qs.filter(genres__id=gid)
        qs = qs.distinct()

    # Мультиселект: авторы
    author_ids = g.getlist("author")
    if author_ids:
        for aid in author_ids:
            qs = qs.filter(authors__id=aid)
        qs = qs.distinct()

    # Мультиселект: языки
    language_ids = g.getlist("language")
    if language_ids:
        qs = qs.filter(language__id__in=language_ids)

    # Диапазон: год публикации
    year_from = g.get("year_from", "").strip()
    year_to   = g.get("year_to",   "").strip()
    if year_from.isdigit():
        qs = qs.filter(publication_year__gte=int(year_from))
    if year_to.isdigit():
        qs = qs.filter(publication_year__lte=int(year_to))

    # Диапазон: страниц
    pages_from = g.get("pages_from", "").strip()
    pages_to   = g.get("pages_to",   "").strip()
    if pages_from.isdigit():
        qs = qs.filter(pages__gte=int(pages_from))
    if pages_to.isdigit():
        qs = qs.filter(pages__lte=int(pages_to))

    # Диапазон: средняя цена
    price_from = g.get("price_from", "").strip()
    price_to   = g.get("price_to",   "").strip()
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

    # Рейтинг минимум (слайдер)
    rating_min = g.get("rating_min", "").strip()
    if rating_min:
        try:
            qs = qs.filter(avg_rating__gte=float(rating_min))
        except ValueError:
            pass

    # Сортировка
    ordering = g.get("ordering", "-avg_rating")
    if ordering in {"-avg_rating", "-rating_count", "-publication_year",
                    "publication_year", "avg_price", "-avg_price"}:
        qs = qs.order_by(ordering)

    paginator = Paginator(qs, settings.BOOKS_PER_PAGE)
    page      = paginator.get_page(g.get("page", 1))

    params = request.GET.copy()
    params.pop("page", None)

    # Агрегаты для диапазонов
    agg = Book.objects.aggregate(
        min_year=Min("publication_year"), max_year=Max("publication_year"),
        min_pages=Min("pages"), max_pages=Max("pages"),
        min_price=Min("avg_price"), max_price=Max("avg_price"),
    )

    has_filters = any([
        search, genre_ids, author_ids, language_ids,
        year_from, year_to, pages_from, pages_to,
        price_from, price_to, rating_min,
    ])

    ctx = {
        "books":           page,
        "total":           paginator.count,
        "query_string":    params.urlencode(),
        "has_filters":     has_filters,
        # Данные для фильтров
        "all_genres":      Genre.objects.all(),
        "all_authors":     Author.objects.all()[:200],
        "all_languages":   Language.objects.all(),
        "selected_genres":   genre_ids,
        "selected_authors":  author_ids,
        "selected_languages": language_ids,
        "agg": agg,
        # Текущие значения
        "f": g,
    }
    if request.htmx:
        return render(request, "books/_book_list.html", ctx)
    return render(request, "books/catalog.html", ctx)



# ── Страница книги ────────────────────────────────────────────────────────────

def book_detail(request, pk):
    book = get_object_or_404(
        Book.objects.prefetch_related("authors", "genres", "store_links__store"),
        pk=pk
    )
    from reviews.models import Review
    from .recommendations import similar_books as get_similar, also_read as get_also_read
    from .models import Genre, Author, Language, Publisher, Series, Quote, ReadingProgress, PriceAlert
    reviews = Review.objects.filter(book=book, status=Review.APPROVED).select_related("user")

    user_lists    = []
    book_list_ids = set()
    if request.user.is_authenticated:
        user_lists    = UserList.objects.filter(user=request.user)
        book_list_ids = set(
            UserList.objects.filter(user=request.user, books=book).values_list("id", flat=True)
        )

    store_links     = list(book.store_links.select_related("store").filter(store__is_active=True))
    linked_ids      = {sl.store_id for sl in store_links}
    unlinked_stores = [s for s in Store.objects.filter(is_active=True) if s.id not in linked_ids]

    # Данные для инлайн-редактирования (только staff)
    edit_author_ids = "[" + ",".join(str(a.pk) for a in book.authors.all()) + "]"
    edit_genre_ids  = "[" + ",".join(str(g.pk) for g in book.genres.all()) + "]"

    ctx = {
        "book":            book,
        "reviews":         reviews,
        "review_count":    reviews.count(),
        "user_lists":      user_lists,
        "book_list_ids":   book_list_ids,
        "store_links":     store_links,
        "unlinked_stores": unlinked_stores,
        "similar":         get_similar(book, limit=5),
        "also_read":       get_also_read(book, limit=6),
        "user_has_review": reviews.filter(user=request.user).exists()
                           if request.user.is_authenticated else False,
        "active_tab":      request.GET.get("tab", "about"),
        # Цитаты
        "quotes":          Quote.objects.filter(book=book).select_related("user"),
        "quotes_count":    Quote.objects.filter(book=book).count(),
        # Прогресс чтения и алерт цены текущего пользователя
        "reading_progress": (
            ReadingProgress.objects.filter(user=request.user, book=book).first()
            if request.user.is_authenticated else None
        ),
        "user_price_alert": (
            PriceAlert.objects.filter(user=request.user, book=book).first()
            if request.user.is_authenticated else None
        ),
        # Для формы редактирования
        "all_authors":          Author.objects.order_by("name"),
        "all_genres":           Genre.objects.order_by("name"),
        "all_languages":        Language.objects.order_by("name"),
        "all_publishers":       Publisher.objects.order_by("name"),
        "all_series":           Series.objects.order_by("name"),
        "edit_author_ids":      edit_author_ids,
        "edit_genre_ids":       edit_genre_ids,
        "edit_publisher_id":    book.publisher_id,
        "edit_publisher_name":  book.publisher.name if book.publisher else "",
        "edit_series_id":       book.series_id,
        "edit_series_name":     book.series.name if book.series else "",
    }
    return render(request, "books/book_detail.html", ctx)


# ── Управление списками ────────────────────────────────────────────────────────

@login_required
def toggle_list(request):
    """HTMX POST — переключить книгу в списке пользователя."""
    if request.method != "POST":
        return HttpResponse(status=405)
    book      = get_object_or_404(Book, pk=request.POST.get("book_id"))
    list_id   = request.POST.get("list_id")
    user_list = get_object_or_404(UserList, pk=list_id, user=request.user)

    if user_list.books.filter(pk=book.pk).exists():
        user_list.books.remove(book)
    else:
        user_list.books.add(book)

    # Инвалидируем AI-кеш рекомендаций — вкус пользователя изменился
    from books.ai_recommendations import invalidate
    invalidate(request.user.pk)

    # Обновлённое состояние всех списков
    book_list_ids = set(
        UserList.objects.filter(user=request.user, books=book).values_list("id", flat=True)
    )
    user_lists = UserList.objects.filter(user=request.user)
    return render(request, "books/_list_dropdown.html", {
        "book": book, "user_lists": user_lists, "book_list_ids": book_list_ids
    })


# ── Запрос цены + поллинг ─────────────────────────────────────────────────────

@login_required
def request_price(request, pk):
    """HTMX POST — проверить reCAPTCHA, запустить задачу, вернуть блок с поллингом."""
    if request.method != "POST":
        return HttpResponse(status=405)

    book = get_object_or_404(Book, pk=pk)

    # ── reCAPTCHA v2 verification ─────────────────────────────────────────────
    import requests as http_req
    from django.conf import settings

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

    from .tasks import scrape_book_prices
    result = scrape_book_prices.delay(book.pk)
    request.session[f"price_task_{book.pk}"] = result.id

    return render(request, "books/_price_block.html", {
        "book": book, "pending": True, "task_id": result.id
    })


@login_required
def price_captcha(request, pk):
    """GET — показать форму с reCAPTCHA перед запросом цены."""
    from django.conf import settings as conf
    book = get_object_or_404(Book, pk=pk)
    return render(request, "books/_price_captcha.html", {
        "book": book,
        "recaptcha_site_key": conf.RECAPTCHA_PUBLIC_KEY,
    })


def price_status(request, pk):
    """
    HTMX GET — опросить статус задачи Celery.
    Вызывается каждые 2 сек через hx-trigger="every 2s".
    Когда задача завершена — возвращает финальный блок без поллинга.
    """
    book    = get_object_or_404(Book, pk=pk)
    task_id = request.GET.get("task_id") or request.session.get(f"price_task_{book.pk}")

    if task_id:
        from celery.result import AsyncResult
        result = AsyncResult(task_id)
        done   = result.ready()
    else:
        done = True  # нет задачи — показать текущее состояние

    if done:
        # Перечитываем книгу из БД — Celery уже записал avg_price
        book.refresh_from_db()
        return render(request, "books/_price_block.html", {
            "book": book, "pending": False
        })

    # Ещё выполняется — вернуть тот же блок с поллингом
    return render(request, "books/_price_block.html", {
        "book": book, "pending": True, "task_id": task_id
    })


def price_chart_data(request, pk):
    """
    JSON — данные для графика цен.
    Агрегируем BookPrice по дням: для каждого магазина отдельная линия
    + общая средняя.
    """
    from django.http import JsonResponse
    from django.db.models.functions import TruncDate
    from django.db.models import Avg
    from .models import BookPrice, BookStore

    book = get_object_or_404(Book, pk=pk)

    # По каждому магазину — средняя цена за день
    store_links = BookStore.objects.filter(book=book).select_related("store")

    datasets = []
    all_dates = set()

    # Цвета для линий магазинов
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
            "label":       link.store.name,
            "data":        data,
            "color":       palette[i % len(palette)],
            "borderDash":  [],
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
        "label":      "Средняя",
        "data":       avg_data,
        "color":      "#111111",
        "borderDash": [6, 3],
    })

    # Нормализуем: для каждого датасета — список значений по labels (None если нет)
    for ds in datasets:
        ds["points"] = [ds["data"].get(l) for l in labels]
        del ds["data"]

    return JsonResponse({"labels": labels, "datasets": datasets})



def author_detail(request, pk):
    from django.db.models import Q, Min, Max
    from .models import Author, Language

    author = get_object_or_404(Author.objects.prefetch_related("books"), pk=pk)
    g = request.GET

    qs = author.books.prefetch_related("authors", "genres").select_related("publisher", "language")

    # Фильтры — такие же как в каталоге
    search = g.get("search", "").strip()
    if search:
        qs = qs.filter(Q(title__icontains=search) | Q(description__icontains=search))

    genre_ids = g.getlist("genre")
    if genre_ids:
        for gid in genre_ids:
            qs = qs.filter(genres__id=gid)
        qs = qs.distinct()

    year_from = g.get("year_from", "").strip()
    year_to   = g.get("year_to",   "").strip()
    if year_from.isdigit(): qs = qs.filter(publication_year__gte=int(year_from))
    if year_to.isdigit():   qs = qs.filter(publication_year__lte=int(year_to))

    rating_min = g.get("rating_min", "").strip()
    if rating_min:
        try: qs = qs.filter(avg_rating__gte=float(rating_min))
        except ValueError: pass

    ordering = g.get("ordering", "-avg_rating")
    if ordering in {"-avg_rating", "-rating_count", "-publication_year", "publication_year"}:
        qs = qs.order_by(ordering)

    paginator = Paginator(qs, settings.BOOKS_PER_PAGE)
    page      = paginator.get_page(g.get("page", 1))
    params    = request.GET.copy(); params.pop("page", None)

    agg = author.books.aggregate(
        min_year=Min("publication_year"), max_year=Max("publication_year"),
    )

    # Подписка
    is_subscribed = False
    if request.user.is_authenticated:
        from users.models import AuthorSubscription
        is_subscribed = AuthorSubscription.objects.filter(
            user=request.user, author=author
        ).exists()

    from .models import Genre as GenreModel
    ctx = {
        "author":          author,
        "books":           page,
        "total":           paginator.count,
        "query_string":    params.urlencode(),
        "has_filters":     bool(search or genre_ids or year_from or year_to or rating_min),
        "all_genres":      GenreModel.objects.filter(books__authors=author).distinct(),
        "selected_genres": genre_ids,
        "agg":             agg,
        "f":               g,
        "is_subscribed":   is_subscribed,
    }
    if request.htmx:
        return render(request, "books/_book_list.html", ctx)
    return render(request, "books/author_detail.html", ctx)


@login_required
def toggle_subscribe_author(request, pk):
    """HTMX POST — подписаться/отписаться от автора."""
    if request.method != "POST":
        return HttpResponse(status=405)
    from .models import Author
    from users.models import AuthorSubscription
    author = get_object_or_404(Author, pk=pk)
    sub, created = AuthorSubscription.objects.get_or_create(
        user=request.user, author=author
    )
    if not created:
        sub.delete()
        is_subscribed = False
    else:
        is_subscribed = True
    return render(request, "books/_subscribe_btn.html", {
        "author": author, "is_subscribed": is_subscribed
    })



@user_passes_test(lambda u: u.is_staff)
def store_link_save(request, book_id):
    """HTMX POST — добавить или обновить URL книги в магазине."""
    if request.method != "POST":
        return HttpResponse(status=405)
    book  = get_object_or_404(Book, pk=book_id)
    store = get_object_or_404(Store, pk=request.POST.get("store_id"))
    url   = request.POST.get("product_url", "").strip()
    if not url:
        return HttpResponse(status=400)
    BookStore.objects.update_or_create(
        book=book, store=store,
        defaults={"product_url": url},
    )
    store_links     = list(book.store_links.select_related("store").filter(store__is_active=True))
    linked_ids      = {sl.store_id for sl in store_links}
    unlinked_stores = [s for s in Store.objects.filter(is_active=True) if s.id not in linked_ids]
    return render(request, "books/_store_links.html", {
        "book": book, "store_links": store_links, "unlinked_stores": unlinked_stores
    })


@user_passes_test(lambda u: u.is_staff)
def store_link_delete(request, book_id, store_id):
    """HTMX DELETE — убрать связь книги с магазином."""
    if request.method != "POST":
        return HttpResponse(status=405)
    BookStore.objects.filter(book_id=book_id, store_id=store_id).delete()
    book            = get_object_or_404(Book, pk=book_id)
    store_links     = list(book.store_links.select_related("store").filter(store__is_active=True))
    linked_ids      = {sl.store_id for sl in store_links}
    unlinked_stores = [s for s in Store.objects.filter(is_active=True) if s.id not in linked_ids]
    return render(request, "books/_store_links.html", {
        "book": book, "store_links": store_links, "unlinked_stores": unlinked_stores
    })


# ── Admin partials ────────────────────────────────────────────────────────────

@user_passes_test(lambda u: u.is_staff)
def admin_delete_book(request, pk):
    if request.method != "POST":
        return HttpResponse(status=405)
    get_object_or_404(Book, pk=pk).delete()
    return HttpResponse("")


@user_passes_test(lambda u: u.is_staff)
def admin_books_partial(request):
    q  = request.GET.get("q", "")
    qs = Book.objects.prefetch_related("authors", "genres")
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(authors__name__icontains=q)).distinct()
    return render(request, "books/_admin_books.html", {"books": qs[:50]})


# ── Добавление книги администратором ─────────────────────────────────────────

def _book_add_context():
    """Общий контекст для формы добавления книги."""
    from .models import Genre, Author, Language, Publisher, Series
    return {
        "all_genres":      Genre.objects.order_by("name"),
        "all_authors":     Author.objects.order_by("name"),
        "all_languages":   Language.objects.order_by("name"),
        "all_publishers":  Publisher.objects.order_by("name"),
        "all_series":      Series.objects.order_by("name"),
    }


@user_passes_test(lambda u: u.is_staff)
def book_add(request):
    """Форма добавления книги. GET поддерживает ?copy_from=<pk>."""
    from .models import Genre, Author, Language, Publisher, Series

    # «Добавить копированием» — предзаполнить данные
    copy_from = None
    form_data = {}
    selected_author_ids  = "[]"
    selected_genre_ids   = "[]"
    selected_publisher_id = None
    selected_series_id    = None

    copy_pk = request.GET.get("copy_from") or request.POST.get("copy_from")
    if copy_pk:
        try:
            copy_from = Book.objects.prefetch_related("authors", "genres").get(pk=copy_pk)
            form_data = {
                "title":            copy_from.title + " (копия)",
                "isbn":             "",
                "description":      copy_from.description,
                "publication_year": copy_from.publication_year,
                "pages":            copy_from.pages,
                "language_id":      copy_from.language_id,
                "publisher_name":   copy_from.publisher.name if copy_from.publisher else "",
                "series_name":      copy_from.series.name if copy_from.series else "",
            }
            selected_author_ids    = "[" + ",".join(str(a.pk) for a in copy_from.authors.all()) + "]"
            selected_genre_ids     = "[" + ",".join(str(g.pk) for g in copy_from.genres.all()) + "]"
            selected_publisher_id  = copy_from.publisher_id if copy_from.publisher else None
            selected_series_id     = copy_from.series_id if copy_from.series else None
        except Book.DoesNotExist:
            pass

    if request.method == "POST":
        title = request.POST.get("title", "").strip()
        if not title:
            pub_id = request.POST.get("publisher_id", "").strip()
            ser_id = request.POST.get("series_id", "").strip()
            ctx = _book_add_context()
            ctx.update({
                "error":               "Название обязательно",
                "form_data":           request.POST,
                "copy_from":           copy_from,
                "selected_author_ids": "[" + ",".join(request.POST.getlist("authors")) + "]",
                "selected_genre_ids":  "[" + ",".join(request.POST.getlist("genres")) + "]",
                "selected_publisher_id": int(pub_id) if pub_id.isdigit() else None,
                "selected_series_id":    int(ser_id) if ser_id.isdigit() else None,
            })
            return render(request, "books/book_add.html", ctx)

        # Издательство — сначала пробуем по ID, потом по имени
        publisher = None
        publisher_id = request.POST.get("publisher_id", "").strip()
        publisher_name = request.POST.get("publisher_name", "").strip()
        if publisher_id and publisher_id.isdigit():
            publisher = Publisher.objects.filter(pk=publisher_id).first()
        if not publisher and publisher_name:
            publisher, _ = Publisher.objects.get_or_create(name=publisher_name)

        # Серия — то же самое
        series = None
        series_id = request.POST.get("series_id", "").strip()
        series_name = request.POST.get("series_name", "").strip()
        if series_id and series_id.isdigit():
            series = Series.objects.filter(pk=series_id).first()
        if not series and series_name:
            series, _ = Series.objects.get_or_create(name=series_name)

        language_id = request.POST.get("language")
        language = Language.objects.filter(pk=language_id).first() if language_id else None

        pub_year = request.POST.get("publication_year", "").strip()
        pages    = request.POST.get("pages", "").strip()

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
        genre_ids  = request.POST.getlist("genres")
        if author_ids:
            book.authors.set(Author.objects.filter(pk__in=author_ids))
        if genre_ids:
            book.genres.set(Genre.objects.filter(pk__in=genre_ids))

        messages.success(request, f"Книга «{book.title}» добавлена.")
        return redirect("book_detail", pk=book.pk)

    ctx = _book_add_context()
    ctx.update({
        "copy_from":             copy_from,
        "form_data":             form_data,
        "selected_author_ids":   selected_author_ids,
        "selected_genre_ids":    selected_genre_ids,
        "selected_publisher_id": selected_publisher_id,
        "selected_series_id":    selected_series_id,
    })
    return render(request, "books/book_add.html", ctx)


@user_passes_test(lambda u: u.is_staff)
def book_edit(request, pk):
    """POST — сохранить инлайн-редактирование книги."""
    from .models import Genre, Author, Language, Publisher, Series
    book = get_object_or_404(Book, pk=pk)

    if request.method != "POST":
        return HttpResponse(status=405)

    title = request.POST.get("title", "").strip()
    if not title:
        messages.error(request, "Название не может быть пустым.")
        return redirect("book_detail", pk=pk)

    # Издательство
    publisher = None
    pub_id   = request.POST.get("publisher_id", "").strip()
    pub_name = request.POST.get("publisher_name", "").strip()
    if pub_id and pub_id.isdigit():
        publisher = Publisher.objects.filter(pk=pub_id).first()
    if not publisher and pub_name:
        publisher, _ = Publisher.objects.get_or_create(name=pub_name)

    # Серия
    series = None
    ser_id   = request.POST.get("series_id", "").strip()
    ser_name = request.POST.get("series_name", "").strip()
    if ser_id and ser_id.isdigit():
        series = Series.objects.filter(pk=ser_id).first()
    if not series and ser_name:
        series, _ = Series.objects.get_or_create(name=ser_name)

    language_id = request.POST.get("language")
    language = Language.objects.filter(pk=language_id).first() if language_id else None

    pub_year = request.POST.get("publication_year", "").strip()
    pages    = request.POST.get("pages", "").strip()

    book.title            = title
    book.isbn             = request.POST.get("isbn", "").strip() or None
    book.description      = request.POST.get("description", "").strip()
    book.publication_year = int(pub_year) if pub_year.isdigit() else None
    book.pages            = int(pages)    if pages.isdigit()    else None
    book.publisher        = publisher
    book.series           = series
    book.language         = language
    book.save()

    if "cover_image" in request.FILES:
        book.cover_image = request.FILES["cover_image"]
        book.save(update_fields=["cover_image"])

    author_ids = request.POST.getlist("authors")
    genre_ids  = request.POST.getlist("genres")
    book.authors.set(Author.objects.filter(pk__in=author_ids))
    book.genres.set(Genre.objects.filter(pk__in=genre_ids))

    messages.success(request, f"Книга «{book.title}» обновлена.")
    return redirect("book_detail", pk=pk)


@user_passes_test(lambda u: u.is_staff)
def author_create_inline(request):
    """POST — создать автора прямо в форме книги, возвращает JSON."""
    if request.method != "POST":
        return HttpResponse(status=405)
    from .models import Author
    name = request.POST.get("name", "").strip()
    if not name:
        return JsonResponse({"error": "Введите имя"}, status=400)
    author, created = Author.objects.get_or_create(name=name)
    return JsonResponse({"id": author.pk, "name": author.name, "created": created})


@user_passes_test(lambda u: u.is_staff)
def genre_create_inline(request):
    """POST — создать жанр прямо в форме книги, возвращает JSON."""
    if request.method != "POST":
        return HttpResponse(status=405)
    from .models import Genre
    name = request.POST.get("name", "").strip()
    if not name:
        return JsonResponse({"error": "Введите название"}, status=400)
    genre, created = Genre.objects.get_or_create(name=name)
    return JsonResponse({"id": genre.pk, "name": genre.name, "created": created})


@user_passes_test(lambda u: u.is_staff)
def publisher_create_inline(request):
    """JSON POST — создать издательство прямо в форме книги."""
    if request.method != "POST":
        return HttpResponse(status=405)
    from .models import Publisher
    name = request.POST.get("name", "").strip()
    if not name:
        return JsonResponse({"error": "Введите название"}, status=400)
    publisher, created = Publisher.objects.get_or_create(name=name)
    return JsonResponse({"id": publisher.pk, "name": publisher.name, "created": created})


@user_passes_test(lambda u: u.is_staff)
def series_create_inline(request):
    """JSON POST — создать серию прямо в форме книги."""
    if request.method != "POST":
        return HttpResponse(status=405)
    from .models import Series
    name = request.POST.get("name", "").strip()
    if not name:
        return JsonResponse({"error": "Введите название"}, status=400)
    series, created = Series.objects.get_or_create(name=name)
    return JsonResponse({"id": series.pk, "name": series.name, "created": created})


# ── Прогресс чтения ───────────────────────────────────────────────────────────

@login_required
def reading_progress_save(request, pk):
    """HTMX POST — сохранить текущую страницу."""
    if request.method != "POST":
        return HttpResponse(status=405)
    from .models import ReadingProgress
    book = get_object_or_404(Book, pk=pk)
    page = request.POST.get("current_page", "0").strip()
    if not page.isdigit():
        return HttpResponse(status=400)
    page = min(int(page), book.pages or 999999)
    progress, _ = ReadingProgress.objects.update_or_create(
        user=request.user, book=book,
        defaults={"current_page": page},
    )
    percent = progress.percent()
    return JsonResponse({"current_page": progress.current_page, "percent": percent})


# ── Цитаты ────────────────────────────────────────────────────────────────────

@login_required
def quote_add(request, pk):
    """HTMX POST — добавить цитату."""
    if request.method != "POST":
        return HttpResponse(status=405)
    from .models import Quote
    book = get_object_or_404(Book, pk=pk)
    text = request.POST.get("text", "").strip()
    if not text:
        return HttpResponse(status=400)
    page_raw = request.POST.get("page_number", "").strip()
    page = int(page_raw) if page_raw.isdigit() else None
    Quote.objects.create(user=request.user, book=book, text=text, page_number=page)
    quotes = Quote.objects.filter(book=book).select_related("user")
    return render(request, "books/_quotes.html", {"book": book, "quotes": quotes})


@login_required
def quote_delete(request, pk, quote_pk):
    """HTMX POST — удалить свою цитату."""
    if request.method != "POST":
        return HttpResponse(status=405)
    from .models import Quote
    book = get_object_or_404(Book, pk=pk)
    get_object_or_404(Quote, pk=quote_pk, user=request.user).delete()
    quotes = Quote.objects.filter(book=book).select_related("user")
    return render(request, "books/_quotes.html", {"book": book, "quotes": quotes})


def quotes_partial(request, pk):
    """HTMX GET — список цитат книги."""
    from .models import Quote
    book   = get_object_or_404(Book, pk=pk)
    quotes = Quote.objects.filter(book=book).select_related("user")
    return render(request, "books/_quotes.html", {"book": book, "quotes": quotes})


# ── Алерт цены ────────────────────────────────────────────────────────────────

@login_required
def price_alert_save(request, pk):
    """HTMX POST — установить/обновить алерт цены."""
    if request.method != "POST":
        return HttpResponse(status=405)
    from .models import PriceAlert
    book      = get_object_or_404(Book, pk=pk)
    threshold = request.POST.get("threshold", "").strip().replace(",", ".")
    try:
        threshold = float(threshold)
    except ValueError:
        return HttpResponse("Некорректное значение", status=400)
    PriceAlert.objects.update_or_create(
        user=request.user, book=book,
        defaults={"threshold": threshold, "triggered_at": None},
    )
    alert = PriceAlert.objects.get(user=request.user, book=book)
    return render(request, "books/_price_alert.html", {"book": book, "alert": alert})


@login_required
def price_alert_delete(request, pk):
    """HTMX POST — удалить алерт."""
    if request.method != "POST":
        return HttpResponse(status=405)
    from .models import PriceAlert
    book = get_object_or_404(Book, pk=pk)
    PriceAlert.objects.filter(user=request.user, book=book).delete()
    return render(request, "books/_price_alert.html", {"book": book, "alert": None})
