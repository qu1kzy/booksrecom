from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login, logout
from django.contrib.auth.models import User
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib import messages
from django.http import HttpResponse
from django.db.models import Count, Q
from django.utils import timezone

from .models import UserProfile, AuthorSubscription
from books.models import Book, UserList, Store, BookStore
from reviews.models import Review
from search.models import SearchHistory


# ── Auth ──────────────────────────────────────────────────────────────────────

def register(request):
    if request.user.is_authenticated:
        return redirect("home")
    form = UserCreationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        login(request, user)
        return redirect("onboarding")
    return render(request, "users/register.html", {"form": form})


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


def user_logout(request):
    if request.method == "POST":
        logout(request)
    return redirect("home")


# ── Онбординг ─────────────────────────────────────────────────────────────────

@login_required
def onboarding(request):
    """Страница выбора предпочтений после регистрации."""
    from books.models import Genre, Author
    from django.db.models import Count

    # Если уже прошли онбординг — на главную
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    if profile.onboarding_done and request.method != "POST":
        return redirect("home")

    if request.method == "POST":
        genre_ids  = request.POST.getlist("genres")
        author_ids = request.POST.getlist("authors")
        profile.favorite_genres.set(Genre.objects.filter(pk__in=genre_ids))
        profile.favorite_authors.set(Author.objects.filter(pk__in=author_ids))
        profile.onboarding_done = True
        profile.save(update_fields=["onboarding_done"])
        return redirect("home")

    ctx = {
        "onboarding_genres":  Genre.objects.order_by("name"),
        "onboarding_authors": (
            Author.objects
            .annotate(book_count=Count("books"))
            .filter(book_count__gt=0)
            .order_by("-book_count")[:40]
        ),
    }
    return render(request, "users/onboarding.html", ctx)


# ── Профиль ───────────────────────────────────────────────────────────────────

@login_required
def profile(request):
    u = request.user
    lists = UserList.objects.filter(user=u).prefetch_related("books__authors")

    if request.method == "POST" and "telegram_username" in request.POST:
        username = request.POST.get("telegram_username", "").strip().lstrip("@")
        profile_obj, _ = UserProfile.objects.get_or_create(user=u)
        profile_obj.telegram_username = username
        profile_obj.save(update_fields=["telegram_username"])
        messages.success(request, "Telegram сохранён.")
        return redirect("profile")

    # AI-рекомендации из кеша
    from books.ai_recommendations import load_from_cache
    ai_recs = load_from_cache(u.pk)

    # Обычные рекомендации если AI нет
    from books.recommendations import recommended_for_user
    try:
        recs = recommended_for_user(u, limit=10)
    except Exception:
        recs = []

    ctx = {
        "lists":           lists,
        "my_reviews":      Review.objects.filter(user=u).select_related("book")[:30],
        "search_history":  SearchHistory.objects.filter(user=u)[:20],
        "recommendations": recs,
        "ai_recs":         ai_recs,
        "subscriptions":   u.author_subscriptions.select_related("author"),
    }
    return render(request, "users/profile.html", ctx)


@login_required
def ai_recs_refresh(request, user_id=None):
    """
    HTMX POST — запустить Celery-задачу генерации AI-рекомендаций.
    Возвращает блок с поллингом.
    """
    if request.method != "POST":
        return HttpResponse(status=405)

    from .tasks import generate_ai_recommendations_task
    task = generate_ai_recommendations_task.delay(request.user.pk)
    request.session["ai_recs_task"] = task.id

    return render(request, "users/_ai_recs_block.html", {
        "pending": True, "task_id": task.id
    })


@login_required
def ai_recs_status(request):
    """
    HTMX GET — опросить статус задачи и вернуть готовый блок.
    Вызывается через hx-trigger='every 2s' пока задача не завершена.
    """
    task_id = request.GET.get("task_id") or request.session.get("ai_recs_task")

    done = True
    if task_id:
        from celery.result import AsyncResult
        done = AsyncResult(task_id).ready()

    if done:
        from books.ai_recommendations import load_from_cache
        ai_recs = load_from_cache(request.user.pk)
        return render(request, "users/_ai_recs_block.html", {
            "pending": False, "ai_recs": ai_recs
        })

    return render(request, "users/_ai_recs_block.html", {
        "pending": True, "task_id": task_id
    })


@login_required
def save_telegram(request):
    """HTMX POST — сохранить Telegram username."""
    if request.method != "POST":
        return HttpResponse(status=405)
    username = request.POST.get("telegram_username", "").strip().lstrip("@")
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    profile.telegram_username = username
    profile.save(update_fields=["telegram_username"])
    return render(request, "users/_telegram_block.html", {
        "profile": profile, "saved": True
    })


@login_required
def create_list(request):
    """HTMX POST — создать новый список."""
    if request.method != "POST":
        return HttpResponse(status=405)
    name = request.POST.get("name", "").strip()
    if not name:
        return HttpResponse(status=400)
    if UserList.objects.filter(user=request.user, name=name).exists():
        messages.error(request, f"Список «{name}» уже существует.")
    else:
        UserList.objects.create(user=request.user, name=name)
        # Инвалидируем AI-кеш — списки изменились
        from books.ai_recommendations import invalidate
        invalidate(request.user.pk)
    lists = UserList.objects.filter(user=request.user).prefetch_related("books__authors")
    return render(request, "users/_lists_panel.html", {"lists": lists})


@login_required
def delete_list(request, list_id):
    """HTMX POST — удалить список (кроме дефолтного)."""
    if request.method != "POST":
        return HttpResponse(status=405)
    ul = get_object_or_404(UserList, pk=list_id, user=request.user)
    if ul.is_default:
        return HttpResponse(status=403)
    ul.delete()
    # Инвалидируем AI-кеш
    from books.ai_recommendations import invalidate
    invalidate(request.user.pk)
    lists = UserList.objects.filter(user=request.user).prefetch_related("books__authors")
    return render(request, "users/_lists_panel.html", {"lists": lists})


# ── Администратор ─────────────────────────────────────────────────────────────

@user_passes_test(lambda u: u.is_staff)
def admin_panel(request):
    raw = (SearchHistory.objects.values("query")
           .annotate(cnt=Count("query")).order_by("-cnt")[:8])
    max_cnt = raw[0]["cnt"] if raw else 1
    popular_queries = [{"query": r["query"], "pct": int(r["cnt"] / max_cnt * 100)} for r in raw]

    ctx = {
        "stat_books":      Book.objects.count(),
        "stat_users":      User.objects.count(),
        "stat_reviews":    Review.objects.count(),
        "stat_searches":   SearchHistory.objects.filter(
            created_at__date=timezone.now().date()).count(),
        "popular_books":   Book.objects.order_by("-rating_count")[:8],
        "popular_queries": popular_queries,
        "users":           User.objects.select_related("profile").order_by("-date_joined")[:50],
        "books":           Book.objects.prefetch_related("authors")[:50],
        "pending_reviews": Review.objects.filter(status=Review.PENDING).select_related("user", "book"),
        "stores":          Store.objects.annotate(link_count=Count("book_links")),
    }
    return render(request, "users/admin_panel.html", ctx)


@user_passes_test(lambda u: u.is_staff)
def admin_users_partial(request):
    q  = request.GET.get("q", "")
    qs = User.objects.select_related("profile").order_by("-date_joined")
    if q:
        qs = qs.filter(Q(username__icontains=q) | Q(email__icontains=q))
    return render(request, "users/_admin_users.html", {"users": qs[:50]})


@user_passes_test(lambda u: u.is_staff)
def admin_block_user(request, user_id):
    if request.method != "POST":
        return HttpResponse(status=405)
    target = get_object_or_404(User, pk=user_id)
    if target.is_staff:
        return HttpResponse(status=403)
    p, _ = UserProfile.objects.get_or_create(user=target)
    p.is_blocked = True
    p.blocked_until = None
    p.save()
    target.refresh_from_db()
    return render(request, "users/_user_card.html", {"u": target})


@user_passes_test(lambda u: u.is_staff)
def admin_unblock_user(request, user_id):
    if request.method != "POST":
        return HttpResponse(status=405)
    target = get_object_or_404(User, pk=user_id)
    p, _ = UserProfile.objects.get_or_create(user=target)
    p.is_blocked = False
    p.blocked_until = None
    p.save()
    target.refresh_from_db()
    return render(request, "users/_user_card.html", {"u": target})


# ── Магазины (admin) ──────────────────────────────────────────────────────────

@user_passes_test(lambda u: u.is_staff)
def admin_store_save(request):
    """HTMX POST — создать или изменить магазин."""
    if request.method != "POST":
        return HttpResponse(status=405)
    store_id = request.POST.get("store_id")
    data = {
        "name":           request.POST.get("name", "").strip(),
        "base_url":       request.POST.get("base_url", "").strip(),
        "icon":           request.POST.get("icon", "").strip(),
        "price_selector": request.POST.get("price_selector", "").strip(),
        "is_active":      request.POST.get("is_active") == "on",
    }
    if not data["name"] or not data["base_url"]:
        return HttpResponse("Название и URL обязательны", status=400)

    if store_id:
        Store.objects.filter(pk=store_id).update(**data)
    else:
        Store.objects.create(**data)

    stores = Store.objects.annotate(link_count=Count("book_links"))
    return render(request, "users/_admin_stores.html", {"stores": stores})


@user_passes_test(lambda u: u.is_staff)
def admin_store_delete(request, store_id):
    if request.method != "POST":
        return HttpResponse(status=405)
    get_object_or_404(Store, pk=store_id).delete()
    stores = Store.objects.annotate(link_count=Count("book_links"))
    return render(request, "users/_admin_stores.html", {"stores": stores})


# ── Экспорт списков ───────────────────────────────────────────────────────────

@login_required
def export_lists(request):
    """GET — выгрузить все списки пользователя в JSON."""
    import json
    from django.http import HttpResponse as HR
    lists = UserList.objects.filter(user=request.user).prefetch_related(
        "books__authors", "books__genres"
    )
    data = []
    for ul in lists:
        data.append({
            "list":      ul.name,
            "sentiment": ul.sentiment_tag,
            "books": [
                {
                    "title":   b.title,
                    "authors": [a.name for a in b.authors.all()],
                    "genres":  [g.name for g in b.genres.all()],
                    "isbn":    b.isbn or "",
                    "year":    b.publication_year,
                }
                for b in ul.books.all()
            ],
        })
    payload  = json.dumps(data, ensure_ascii=False, indent=2)
    response = HR(payload, content_type="application/json; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="my_books.json"'
    return response


# ── Публичные списки ──────────────────────────────────────────────────────────

def public_lists(request):
    """Страница популярных публичных списков."""
    from books.models import UserList
    from django.db.models import Count
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


@login_required
def toggle_list_public(request, list_id):
    """HTMX POST — переключить публичность списка."""
    if request.method != "POST":
        return HttpResponse(status=405)
    ul = get_object_or_404(UserList, pk=list_id, user=request.user)
    ul.is_public = not ul.is_public
    ul.save(update_fields=["is_public"])
    lists = UserList.objects.filter(user=request.user).prefetch_related("books__authors")
    return render(request, "users/_lists_panel.html", {"lists": lists})
