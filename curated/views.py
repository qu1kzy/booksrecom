from django.shortcuts import render, redirect, get_object_or_404
from django.http import HttpResponse, JsonResponse
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST, require_GET
from django.core.exceptions import PermissionDenied
from django.db.models import Q, Count
from django.template.loader import render_to_string

from .models import Collection, CollectionBook
from books.models import Book


def _staff_required(view_func):
    def wrapper(request, *args, **kwargs):
        if not request.user.is_staff:
            raise PermissionDenied
        return view_func(request, *args, **kwargs)
    wrapper.__name__ = view_func.__name__
    return wrapper


# ─── ПУБЛИЧНЫЕ ─────────────────────────────────────────────────────────────────

def collections_list(request):
    collections = (
        Collection.objects
        .filter(is_published=True)
        .prefetch_related("items__book")
        .annotate(num_books=Count("items"))
    )
    return render(request, "curated/collection_list.html", {
        "collections": collections,
    })


def collection_detail(request, pk):
    col = get_object_or_404(Collection, pk=pk, is_published=True)
    items = (
        col.items
        .select_related("book")
        .prefetch_related("book__authors", "book__genres")
        .order_by("order")
    )
    return render(request, "curated/collection_detail.html", {
        "collection": col,
        "items": items,
    })


# ─── АДМИНСКИЕ ─────────────────────────────────────────────────────────────────

@login_required
@_staff_required
def collection_create(request):
    if request.method == "POST":
        title = request.POST.get("title", "").strip()
        description = request.POST.get("description", "").strip()
        if title:
            col = Collection.objects.create(
                title=title,
                description=description,
                created_by=request.user,
                cover_image=request.FILES.get("cover_image"),
            )
            return redirect("collection_edit", pk=col.pk)
    return render(request, "curated/collection_create.html")


@login_required
@_staff_required
def collection_edit(request, pk):
    col = get_object_or_404(Collection, pk=pk)
    selected = (
        col.items
        .select_related("book")
        .prefetch_related("book__authors")
        .order_by("order")
    )
    return render(request, "curated/collection_editor.html", {
        "collection": col,
        "selected_items": selected,
    })


@login_required
@_staff_required
@require_POST
def collection_delete(request, pk):
    col = get_object_or_404(Collection, pk=pk)
    col.delete()
    return redirect("collections_list")


@login_required
@_staff_required
@require_POST
def collection_toggle_publish(request, pk):
    col = get_object_or_404(Collection, pk=pk)
    col.is_published = not col.is_published
    col.save(update_fields=["is_published"])
    return render(request, "curated/_editor_header.html", {"collection": col})


@login_required
@_staff_required
@require_POST
def collection_add_book(request, pk):
    col = get_object_or_404(Collection, pk=pk)
    book_id = request.POST.get("book_id")
    book = get_object_or_404(Book, pk=book_id)
    max_order = col.items.count()
    CollectionBook.objects.get_or_create(
        collection=col, book=book,
        defaults={"order": max_order},
    )
    selected = col.items.select_related("book").prefetch_related("book__authors").order_by("order")
    ctx = {"collection": col, "selected_items": selected}

    html = render_to_string("curated/_selected_books.html", ctx, request=request)
    # OOB: убрать книгу из поиска + обновить мобильную панель
    oob_delete = f'<div id="book-option-{book.pk}" hx-swap-oob="delete"></div>'
    oob_mobile = (
        f'<div id="selected-books-mobile" hx-swap-oob="innerHTML">'
        + render_to_string("curated/_selected_books.html", ctx, request=request)
        + '</div>'
    )
    return HttpResponse(html + oob_delete + oob_mobile)


@login_required
@_staff_required
@require_POST
def collection_remove_book(request, pk, book_id):
    col = get_object_or_404(Collection, pk=pk)
    CollectionBook.objects.filter(collection=col, book_id=book_id).delete()
    selected = (
        col.items
        .select_related("book")
        .prefetch_related("book__authors")
        .order_by("order")
    )
    ctx = {"collection": col, "selected_items": selected}
    html = render_to_string("curated/_selected_books.html", ctx, request=request)
    # OOB: обновить мобильную панель
    oob_mobile = (
        f'<div id="selected-books-mobile" hx-swap-oob="innerHTML">'
        + render_to_string("curated/_selected_books.html", ctx, request=request)
        + '</div>'
    )
    resp = HttpResponse(html + oob_mobile)
    # Триггер перезагрузки результатов поиска
    resp["HX-Trigger"] = "refreshSearch"
    return resp


@login_required
@_staff_required
@require_GET
def collection_search_books(request, pk):
    col = get_object_or_404(Collection, pk=pk)
    q = request.GET.get("q", "").strip()
    existing_ids = set(col.items.values_list("book_id", flat=True))

    if q:
        books = (
            Book.objects
            .filter(Q(title__icontains=q) | Q(authors__name__icontains=q))
            .exclude(pk__in=existing_ids)
            .prefetch_related("authors")
            .distinct()[:20]
        )
    else:
        books = (
            Book.objects
            .exclude(pk__in=existing_ids)
            .prefetch_related("authors")
            .order_by("-avg_rating")[:20]
        )

    return render(request, "curated/_search_results.html", {
        "books": books,
        "collection": col,
    })
