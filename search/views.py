from django.shortcuts import render
from django.contrib.postgres.search import SearchVector, SearchQuery, SearchRank, SearchHeadline
from django.db.models import Q, Value, FloatField
from books.models import Book
from .models import SearchHistory


def search(request):
    query   = request.GET.get("q", "").strip()
    results = []

    if query:
        results = _fts_search(query)

        # Fallback на icontains если FTS ничего не нашёл (короткие запросы, опечатки)
        if not results:
            results = list(
                Book.objects
                .filter(
                    Q(title__icontains=query)
                    | Q(authors__name__icontains=query)
                    | Q(genres__name__icontains=query)
                    | Q(description__icontains=query)
                    | Q(isbn__iexact=query)
                )
                .prefetch_related("authors", "genres")
                .distinct()[:30]
            )

        SearchHistory.objects.create(
            user=request.user if request.user.is_authenticated else None,
            query=query,
            results_count=len(results),
        )

    ctx = {"query": query, "results": results}

    if request.htmx:
        return render(request, "search/_results.html", ctx)

    ctx.update({
        "popular": Book.objects.order_by("-rating_count")[:5],
        "newest":  Book.objects.order_by("-publication_year")[:5],
    })
    return render(request, "core/home.html", ctx)


def _fts_search(query: str):
    """
    PostgreSQL Full Text Search с русской морфологией.
    Вес A → title, вес B → authors names, вес C → description.
    """
    try:
        search_query = SearchQuery(query, config="russian", search_type="websearch")
        vector = (
            SearchVector("title", weight="A", config="russian") +
            SearchVector("description", weight="C", config="russian")
        )
        qs = (
            Book.objects
            .annotate(rank=SearchRank(vector, search_query))
            .filter(rank__gte=0.05)
            .order_by("-rank")
            .prefetch_related("authors", "genres")
            .distinct()[:30]
        )

        results = list(qs)

        # Дополнительно ищем по именам авторов через icontains
        # (авторы — в отдельной таблице, FTS по ним дороже)
        author_books = list(
            Book.objects
            .filter(authors__name__icontains=query)
            .exclude(pk__in={b.pk for b in results})
            .prefetch_related("authors", "genres")
            .distinct()[:10]
        )

        return results + author_books

    except Exception:
        # Если PostgreSQL FTS недоступна (например, нет конфига russian) — fallback
        return []
