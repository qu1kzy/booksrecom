from django.shortcuts import render
from django.db.models import Count
from books.models import Book, Genre, Author


def _book_of_the_week():
    """Книга с наибольшим числом добавлений в списки за последние 7 дней."""
    from django.utils import timezone
    from datetime import timedelta
    from books.models import UserList
    week_ago = timezone.now() - timedelta(days=7)
    # Считаем по UserList.books через промежуточную таблицу
    from django.db.models import Count
    result = (
        Book.objects
        .filter(in_lists__created_at__gte=week_ago)
        .annotate(add_count=Count("in_lists"))
        .order_by("-add_count", "-avg_rating")
        .prefetch_related("authors", "genres")
        .first()
    )
    # Если за неделю ничего не добавляли — просто самая рейтинговая
    if not result:
        result = Book.objects.prefetch_related("authors", "genres").order_by("-avg_rating").first()
    return result


def home(request):
    ctx = {
        "popular":         Book.objects.prefetch_related("authors", "genres").order_by("-rating_count")[:6],
        "newest":          Book.objects.prefetch_related("authors", "genres").order_by("-publication_year")[:6],
        "book_of_week":    _book_of_the_week(),
        "query":           request.GET.get("q", ""),
    }

    # Для онбординг-модала: жанры и топ-авторы по количеству книг
    if request.user.is_authenticated:
        profile = getattr(request.user, "profile", None)
        if profile and not profile.onboarding_done:
            ctx["onboarding_genres"]  = Genre.objects.order_by("name")
            ctx["onboarding_authors"] = (
                Author.objects
                .annotate(book_count=Count("books"))
                .filter(book_count__gt=0)
                .order_by("-book_count")[:40]
            )

    return render(request, "core/home.html", ctx)
