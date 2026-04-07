from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login, logout
from django.contrib.auth.models import User
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.contrib.auth.decorators import login_required
from django.contrib.auth.tokens import default_token_generator
from django.contrib import messages
from django.core.mail import send_mail
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.db.models import Count, Q, Avg, Sum
from django.utils import timezone
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from django.utils.encoding import force_bytes, force_str
from django.views.decorators.http import require_GET, require_POST, require_http_methods
from django.core.exceptions import PermissionDenied
from django.core.cache import cache
from django.conf import settings as conf
from django.template.loader import render_to_string
from functools import wraps

import csv
import json
from celery.result import AsyncResult

from .models import UserProfile, AuthorSubscription, Achievement, check_achievements
from books.models import Book, UserList, Store, BookStore, Genre, Author, ReadingProgress
from reviews.models import Review
from search.models import SearchHistory
from books.ai_recommendations import load_from_cache, invalidate as invalidate_ai_cache
from books.recommendations import recommended_for_user
from .tasks import generate_ai_recommendations_task


# ─── RATE LIMITING ────────────────────────────────────────────────────────────

def rate_limit(key_prefix, max_requests=10, period=60):
    """Простой rate-limiter через Django cache. Без внешних зависимостей."""
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            if request.user.is_authenticated:
                rl_key = f"rl:{key_prefix}:{request.user.pk}"
            else:
                rl_key = f"rl:{key_prefix}:{request.META.get('REMOTE_ADDR', '?')}"
            count = cache.get(rl_key, 0)
            if count >= max_requests:
                return HttpResponse(
                    "Слишком много запросов. Попробуйте позже.",
                    status=429,
                )
            cache.set(rl_key, count + 1, period)
            return view_func(request, *args, **kwargs)
        return _wrapped
    return decorator

# ─── ДЕКОРАТОРЫ ───────────────────────────────────────────────────────────────

def staff_required(view_func):
    """Декоратор, разрешающий доступ только персоналу (staff)."""
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_staff:
            raise PermissionDenied
        return view_func(request, *args, **kwargs)
    return _wrapped

# ─── ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ─────────────────────────────────────────────────

def _get_user_profile(user):
    """Возвращает профиль пользователя (создаёт при необходимости)."""
    profile, _ = UserProfile.objects.get_or_create(user=user)
    return profile

def _invalidate_ai_cache(user_id):
    """Инвалидирует кеш AI-рекомендаций пользователя."""
    invalidate_ai_cache(user_id)

def _render_lists_panel(user):
    """Рендерит частичный шаблон со списками пользователя."""
    lists = UserList.objects.filter(user=user).prefetch_related("books__authors")
    return render(None, "users/_lists_panel.html", {"lists": lists})

def _get_task_status(task_id):
    """Проверяет готовность задачи Celery."""
    if not task_id:
        return True, None
    result = AsyncResult(task_id)
    return result.ready(), result

# ─── AUTH ─────────────────────────────────────────────────────────────────────

@require_http_methods(["GET", "POST"])
def register(request):
    if request.user.is_authenticated:
        return redirect("home")
    form = UserCreationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        email = request.POST.get("email", "").strip()
        user = form.save(commit=False)
        user.email = email
        user.is_active = False
        user.save()
        _get_user_profile(user)
        _send_verification_email(user, request)
        return render(request, "users/email_verify_sent.html", {"email": email})
    return render(request, "users/register.html", {"form": form})


def _send_verification_email(user, request):
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    link = request.build_absolute_uri(f"/users/verify-email/{uid}/{token}/")
    send_mail(
        subject="Подтвердите email — Строка",
        message=f"Здравствуйте, {user.username}!\n\nПодтвердите email по ссылке:\n{link}",
        from_email=conf.DEFAULT_FROM_EMAIL,
        recipient_list=[user.email],
        fail_silently=True,
    )


def verify_email(request, uidb64, token):
    try:
        uid = force_str(urlsafe_base64_decode(uidb64))
        user = User.objects.get(pk=uid)
    except (TypeError, ValueError, OverflowError, User.DoesNotExist):
        user = None
    if user and default_token_generator.check_token(user, token):
        user.is_active = True
        user.save(update_fields=["is_active"])
        profile = _get_user_profile(user)
        profile.email_verified = True
        profile.save(update_fields=["email_verified"])
        login(request, user)
        messages.success(request, "Email подтверждён!")
        return redirect("onboarding")
    return render(request, "users/email_verify_invalid.html")

@require_http_methods(["GET", "POST"])
def user_login(request):
    if request.user.is_authenticated:
        return redirect("home")
    form = AuthenticationForm(request, request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.get_user()
        profile = getattr(user, "profile", None)
        if profile and profile.is_currently_blocked:
            messages.error(request, "Ваш аккаунт заблокирован.")
            return render(request, "users/login.html", {"form": form})
        login(request, user)
        next_url = request.GET.get("next", "")
        if not next_url:
            profile = getattr(user, "profile", None)
            if profile and not profile.onboarding_done:
                return redirect("onboarding")
        return redirect(next_url or "home")
    return render(request, "users/login.html", {"form": form})

@require_POST
def user_logout(request):
    logout(request)
    return redirect("home")

# ─── ОНБОРДИНГ ────────────────────────────────────────────────────────────────

@login_required
@require_http_methods(["GET", "POST"])
def onboarding(request):
    profile = _get_user_profile(request.user)

    if request.method == "POST":
        genre_ids = request.POST.getlist("genres")
        author_ids = request.POST.getlist("authors")
        profile.favorite_genres.set(Genre.objects.filter(pk__in=genre_ids))
        profile.favorite_authors.set(Author.objects.filter(pk__in=author_ids))
        profile.onboarding_done = True
        profile.save(update_fields=["onboarding_done"])
        return redirect("home")

    selected_genre_ids = list(profile.favorite_genres.values_list("pk", flat=True))
    selected_author_ids = list(profile.favorite_authors.values_list("pk", flat=True))

    ctx = {
        "onboarding_genres": Genre.objects.order_by("name"),
        "onboarding_authors": (
            Author.objects
            .annotate(book_count=Count("books"))
            .filter(book_count__gt=0)
            .order_by("-book_count")[:40]
        ),
        "selected_genre_ids": selected_genre_ids,
        "selected_author_ids": selected_author_ids,
        "is_returning": profile.onboarding_done,
    }
    return render(request, "users/onboarding.html", ctx)

# ─── ПРОФИЛЬ ──────────────────────────────────────────────────────────────────

@login_required
@require_http_methods(["GET", "POST"])
def profile(request):
    user = request.user
    lists = UserList.objects.filter(user=user).prefetch_related("books__authors")

    if request.method == "POST" and "telegram_username" in request.POST:
        username = request.POST.get("telegram_username", "").strip().lstrip("@")
        profile_obj = _get_user_profile(user)
        profile_obj.telegram_username = username
        profile_obj.save(update_fields=["telegram_username"])
        messages.success(request, "Telegram сохранён.")
        return redirect("profile")

    # AI-рекомендации из кеша
    ai_recs = load_from_cache(user.pk)

    # Обычные рекомендации
    try:
        recs = recommended_for_user(user, limit=10)
    except Exception:
        recs = []

    # Статистика пользователя
    total_books = UserList.objects.filter(user=user).aggregate(
        total=Count("books", distinct=True)
    )["total"] or 0

    total_pages = ReadingProgress.objects.filter(user=user).aggregate(
        total=Sum("current_page")
    )["total"] or 0

    top_genres = (
        Genre.objects
        .filter(books__in_lists__user=user, books__in_lists__sentiment_tag="positive")
        .annotate(cnt=Count("books"))
        .order_by("-cnt")[:3]
    )

    avg_rating_given = (
        Review.objects
        .filter(user=user, status=Review.APPROVED)
        .aggregate(avg=Avg("rating"))["avg"]
    )

    # Достижения (проверяем новые при каждом заходе в профиль)
    new_achievements = check_achievements(user)
    if new_achievements:
        names = [dict(Achievement.TYPES).get(a, a) for a in new_achievements]
        messages.success(request, f"Новое достижение: {', '.join(names)}")
    achievements = user.achievements.order_by("-earned_at")

    my_reviews = Review.objects.filter(user=user).select_related("book")[:30]

    ctx = {
        "lists": lists,
        "my_reviews": my_reviews,
        "search_history": SearchHistory.objects.filter(user=user)[:20],
        "recommendations": recs,
        "ai_recs": ai_recs,
        "subscriptions": user.author_subscriptions.select_related("author"),
        "total_books": total_books,
        "total_pages": total_pages,
        "top_genres": top_genres,
        "avg_rating_given": avg_rating_given,
        "achievements": achievements,
    }
    return render(request, "users/profile.html", ctx)

@login_required
@require_POST
@rate_limit("ai_recs", max_requests=3, period=300)
def ai_recs_refresh(request, user_id=None):
    """Запускает генерацию AI-рекомендаций и возвращает блок с поллингом."""
    task = generate_ai_recommendations_task.delay(request.user.pk)
    request.session["ai_recs_task"] = task.id
    return render(request, "users/_ai_recs_block.html", {
        "pending": True, "task_id": task.id
    })

@login_required
@require_GET
def ai_recs_status(request):
    """Возвращает состояние задачи генерации AI-рекомендаций."""
    task_id = request.GET.get("task_id") or request.session.get("ai_recs_task")
    done, _ = _get_task_status(task_id)

    if done:
        ai_recs = load_from_cache(request.user.pk)
        return render(request, "users/_ai_recs_block.html", {
            "pending": False, "ai_recs": ai_recs
        })
    return render(request, "users/_ai_recs_block.html", {
        "pending": True, "task_id": task_id
    })

# ─── ИМПОРТ БИБЛИОТЕКИ ───────────────────────────────────────────────────────

@login_required
@require_POST
def import_library_view(request):
    """Принимает CSV-файл из Goodreads, запускает фоновый импорт."""
    from books.tasks import import_library_task

    csv_file = request.FILES.get("csv_file")
    if not csv_file:
        return render(request, "users/_import_result.html", {"error": "Файл не выбран"})

    try:
        content = csv_file.read().decode("utf-8")
    except UnicodeDecodeError:
        try:
            csv_file.seek(0)
            content = csv_file.read().decode("cp1251")
        except Exception:
            return render(request, "users/_import_result.html", {"error": "Не удалось прочитать файл"})

    task = import_library_task.delay(request.user.pk, content)
    request.session["import_task"] = task.id
    return render(request, "users/_import_result.html", {
        "pending": True, "task_id": task.id
    })


@login_required
@require_GET
def import_status(request):
    """HTMX polling: статус импорта библиотеки."""
    task_id = request.GET.get("task_id") or request.session.get("import_task")
    if not task_id:
        return render(request, "users/_import_result.html", {"error": "Задача не найдена"})

    from celery.result import AsyncResult
    result = AsyncResult(task_id)

    if result.ready():
        stats = result.result if result.successful() else None
        error = str(result.result) if result.failed() else None
        return render(request, "users/_import_result.html", {
            "pending": False, "stats": stats, "error": error
        })
    return render(request, "users/_import_result.html", {
        "pending": True, "task_id": task_id
    })


@login_required
@require_POST
def save_telegram(request):
    """HTMX — сохранение Telegram username."""
    username = request.POST.get("telegram_username", "").strip().lstrip("@")
    profile = _get_user_profile(request.user)
    profile.telegram_username = username
    profile.save(update_fields=["telegram_username"])
    return render(request, "users/_telegram_block.html", {
        "profile": profile, "saved": True
    })

@login_required
@require_POST
def save_contacts(request):
    """HTMX — сохранение email и Telegram username из модалки."""
    user = request.user
    email = request.POST.get("email", "").strip()
    tg_username = request.POST.get("telegram_username", "").strip().lstrip("@")

    user.email = email
    user.save(update_fields=["email"])

    profile = _get_user_profile(user)
    profile.telegram_username = tg_username
    profile.save(update_fields=["telegram_username"])

    return render(request, "users/_contacts_saved.html", {
        "profile": profile,
        "user": user,
    })

# ─── УПРАВЛЕНИЕ СПИСКАМИ ──────────────────────────────────────────────────────

@login_required
@require_POST
def create_list(request):
    name = request.POST.get("name", "").strip()
    if not name:
        return HttpResponseBadRequest("Название списка не может быть пустым")

    if UserList.objects.filter(user=request.user, name=name).exists():
        messages.error(request, f"Список «{name}» уже существует.")
    else:
        UserList.objects.create(user=request.user, name=name)
        _invalidate_ai_cache(request.user.pk)

    return _render_lists_panel(request.user)

@login_required
@require_POST
def delete_list(request, list_id):
    user_list = get_object_or_404(UserList, pk=list_id, user=request.user)
    if user_list.is_default:
        return HttpResponseBadRequest("Нельзя удалить список по умолчанию")

    user_list.delete()
    _invalidate_ai_cache(request.user.pk)
    return _render_lists_panel(request.user)

@login_required
@require_POST
def toggle_list_public(request, list_id):
    user_list = get_object_or_404(UserList, pk=list_id, user=request.user)
    user_list.is_public = not user_list.is_public
    user_list.save(update_fields=["is_public"])
    return _render_lists_panel(request.user)

# ─── ЭКСПОРТ СПИСКОВ ──────────────────────────────────────────────────────────

@login_required
@require_GET
def export_lists(request):
    lists = UserList.objects.filter(user=request.user).prefetch_related(
        "books__authors", "books__genres"
    )
    data = []
    for ul in lists:
        data.append({
            "list": ul.name,
            "sentiment": ul.sentiment_tag,
            "books": [
                {
                    "title": b.title,
                    "authors": [a.name for a in b.authors.all()],
                    "genres": [g.name for g in b.genres.all()],
                    "isbn": b.isbn or "",
                    "year": b.publication_year,
                }
                for b in ul.books.all()
            ],
        })
    fmt = request.GET.get("format", "json")

    if fmt == "csv":
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="my_books.csv"'
        response.write("\ufeff")  # BOM для корректного открытия в Excel
        writer = csv.writer(response)
        writer.writerow(["Список", "Тональность", "Название", "Авторы", "Жанры", "ISBN", "Год"])
        for ul_data in data:
            for b in ul_data["books"]:
                writer.writerow([
                    ul_data["list"],
                    ul_data["sentiment"],
                    b["title"],
                    ", ".join(b["authors"]),
                    ", ".join(b["genres"]),
                    b["isbn"],
                    b["year"] or "",
                ])
        return response

    payload = json.dumps(data, ensure_ascii=False, indent=2)
    response = HttpResponse(payload, content_type="application/json; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="my_books.json"'
    return response

# ─── ПУБЛИЧНЫЕ СПИСКИ ─────────────────────────────────────────────────────────

@require_GET
def public_lists(request):
    lists = (
        UserList.objects
        .filter(is_public=True)
        .annotate(book_count=Count("books"))
        .filter(book_count__gt=0)
        .select_related("user")
        .prefetch_related("books__authors")
        .order_by("-book_count")[:50]
    )
    return render(request, "users/public_lists.html", {"lists": lists})

# ─── ЭВОЛЮЦИЯ ВКУСА ──────────────────────────────────────────────────────────

@login_required
@require_GET
def taste_data(request):
    """JSON endpoint для Chart.js: данные об эволюции вкуса по месяцам."""
    from django.db.models.functions import TruncMonth, ExtractMonth, ExtractYear
    from collections import defaultdict

    # Все книги пользователя из списков, сгруппированные по месяцу добавления
    list_items = (
        UserList.objects
        .filter(user=request.user)
        .prefetch_related("books__genres")
        .order_by("created_at")
    )

    # Отзывы по месяцам
    reviews = (
        Review.objects
        .filter(user=request.user)
        .order_by("created_at")
    )

    # Агрегация по месяцам
    month_genres = defaultdict(lambda: defaultdict(int))
    month_books_count = defaultdict(int)
    month_ratings = defaultdict(list)

    for ul in list_items:
        month_key = ul.created_at.strftime("%Y-%m")
        for book in ul.books.all():
            month_books_count[month_key] += 1
            for genre in book.genres.all():
                month_genres[month_key][genre.name] += 1

    for r in reviews:
        month_key = r.created_at.strftime("%Y-%m")
        month_ratings[month_key].append(r.rating)

    # Собираем все месяцы
    all_months = sorted(set(list(month_genres.keys()) + list(month_ratings.keys())))
    if not all_months:
        return JsonResponse({"months": [], "genres": {}, "avg_ratings": [], "books_count": []})

    # Топ-5 жанров по суммарной частоте
    total_genre_count = defaultdict(int)
    for m, genres in month_genres.items():
        for g, c in genres.items():
            total_genre_count[g] += c
    top_genres = sorted(total_genre_count.keys(), key=lambda g: total_genre_count[g], reverse=True)[:5]

    genres_data = {}
    for genre in top_genres:
        genres_data[genre] = [month_genres[m].get(genre, 0) for m in all_months]

    avg_ratings = []
    for m in all_months:
        ratings = month_ratings.get(m, [])
        avg_ratings.append(round(sum(ratings) / len(ratings), 1) if ratings else None)

    books_count = [month_books_count.get(m, 0) for m in all_months]

    return JsonResponse({
        "months": all_months,
        "genres": genres_data,
        "avg_ratings": avg_ratings,
        "books_count": books_count,
    })

# ─── АДМИНИСТРИРОВАНИЕ ────────────────────────────────────────────────────────

@require_GET
def user_profile_public(request, username):
    target_user = get_object_or_404(User, username=username)
    public_lists = (
        UserList.objects
        .filter(user=target_user, is_public=True)
        .prefetch_related("books__authors")
        .annotate(book_count=Count("books"))
        .order_by("-book_count")
    )
    approved_reviews = (
        Review.objects
        .filter(user=target_user, status=Review.APPROVED)
        .select_related("book")
        .order_by("-created_at")[:20]
    )
    ctx = {
        "target_user": target_user,
        "target_profile": getattr(target_user, "profile", None),
        "public_lists": public_lists,
        "approved_reviews": approved_reviews,
        "is_own_profile": request.user == target_user,
    }
    if request.user.is_authenticated and not ctx["is_own_profile"]:
        from social.helpers import get_friendship_status
        status, fs = get_friendship_status(request.user, target_user)
        ctx["friendship_status"] = status
        ctx["friendship"] = fs
        ctx["is_sender"] = fs.from_user == request.user if fs else False
    return render(request, "users/user_profile_public.html", ctx)


@staff_required
@require_GET
def admin_panel(request):
    raw = (SearchHistory.objects.values("query")
           .annotate(cnt=Count("query"))
           .order_by("-cnt")[:8])
    max_cnt = raw[0]["cnt"] if raw else 1
    popular_queries = [{"query": r["query"], "pct": int(r["cnt"] / max_cnt * 100)} for r in raw]

    ctx = {
        "stat_books": Book.objects.count(),
        "stat_users": User.objects.count(),
        "stat_reviews": Review.objects.count(),
        "stat_searches": SearchHistory.objects.filter(
            created_at__date=timezone.now().date()
        ).count(),
        "popular_books": Book.objects.order_by("-rating_count")[:8],
        "popular_queries": popular_queries,
        "users": User.objects.select_related("profile").order_by("-date_joined")[:50],
        "books": Book.objects.prefetch_related("authors")[:50],
        "pending_reviews": Review.objects.filter(status=Review.PENDING).select_related("user", "book"),
        "stores": Store.objects.annotate(link_count=Count("book_links")),
    }
    return render(request, "users/admin_panel.html", ctx)

@staff_required
@require_GET
def admin_users_partial(request):
    q = request.GET.get("q", "")
    qs = User.objects.select_related("profile").order_by("-date_joined")
    if q:
        qs = qs.filter(Q(username__icontains=q) | Q(email__icontains=q))
    return render(request, "users/_admin_users.html", {"users": qs[:50]})

@staff_required
@require_POST
def admin_block_user(request, user_id):
    target = get_object_or_404(User, pk=user_id)
    if target.is_staff:
        return HttpResponseBadRequest("Нельзя заблокировать администратора")

    profile = _get_user_profile(target)
    profile.is_blocked = True
    profile.blocked_until = None
    profile.save()
    target.refresh_from_db()
    return render(request, "users/_user_card.html", {"u": target})

@staff_required
@require_POST
def admin_unblock_user(request, user_id):
    target = get_object_or_404(User, pk=user_id)
    profile = _get_user_profile(target)
    profile.is_blocked = False
    profile.blocked_until = None
    profile.save()
    target.refresh_from_db()
    return render(request, "users/_user_card.html", {"u": target})

# ─── УПРАВЛЕНИЕ МАГАЗИНАМИ (ADMIN) ────────────────────────────────────────────

@staff_required
@require_POST
def admin_store_save(request):
    store_id = request.POST.get("store_id")
    data = {
        "name": request.POST.get("name", "").strip(),
        "base_url": request.POST.get("base_url", "").strip(),
        "icon": request.POST.get("icon", "").strip(),
        "price_selector": request.POST.get("price_selector", "").strip(),
        "is_active": request.POST.get("is_active") == "on",
    }
    if not data["name"] or not data["base_url"]:
        return HttpResponseBadRequest("Название и URL обязательны")

    if store_id:
        Store.objects.filter(pk=store_id).update(**data)
    else:
        Store.objects.create(**data)

    stores = Store.objects.annotate(link_count=Count("book_links"))
    return render(request, "users/_admin_stores.html", {"stores": stores})

@staff_required
@require_POST
def admin_store_delete(request, store_id):
    get_object_or_404(Store, pk=store_id).delete()
    stores = Store.objects.annotate(link_count=Count("book_links"))
    return render(request, "users/_admin_stores.html", {"stores": stores})