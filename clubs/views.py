from django.shortcuts import render, redirect, get_object_or_404
from django.http import HttpResponse, JsonResponse
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST, require_GET
from django.db.models import Count, Q

from .models import BookClub, ClubMembership, ClubBook
from books.models import Book
from social.models import ActivityEvent


def clubs_list(request):
    clubs = (
        BookClub.objects
        .filter(is_public=True)
        .annotate(num_members=Count("memberships"))
        .prefetch_related("club_books__book")
    )
    my_club_ids = set()
    if request.user.is_authenticated:
        my_club_ids = set(
            ClubMembership.objects
            .filter(user=request.user)
            .values_list("club_id", flat=True)
        )
    return render(request, "clubs/club_list.html", {
        "clubs": clubs,
        "my_club_ids": my_club_ids,
    })


def club_detail(request, pk):
    club = get_object_or_404(BookClub, pk=pk)
    memberships = club.memberships.select_related("user").order_by("joined_at")
    club_books = club.club_books.select_related("book").prefetch_related("book__authors").order_by("order")
    current_book = club_books.filter(is_current=True).first()

    membership = None
    chat_room = None
    if request.user.is_authenticated:
        membership = ClubMembership.objects.filter(club=club, user=request.user).first()
        # Получаем или создаём чат-комнату клуба
        from chat.models import ChatRoom, ChatParticipant
        chat_room = ChatRoom.objects.filter(room_type="club", club=club).first()
        if not chat_room and membership:
            chat_room = ChatRoom.objects.create(room_type="club", club=club)
        if chat_room and membership:
            ChatParticipant.objects.get_or_create(room=chat_room, user=request.user)

    return render(request, "clubs/club_detail.html", {
        "club": club,
        "memberships": memberships,
        "club_books": club_books,
        "current_book": current_book,
        "membership": membership,
        "chat_room": chat_room,
    })


@login_required
def club_create(request):
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        description = request.POST.get("description", "").strip()
        if name:
            club = BookClub.objects.create(
                name=name,
                description=description,
                created_by=request.user,
                cover_image=request.FILES.get("cover_image"),
            )
            ClubMembership.objects.create(club=club, user=request.user, role="owner")
            return redirect("club_detail", pk=club.pk)
    return render(request, "clubs/club_create.html")


@login_required
@require_POST
def club_join(request, pk):
    club = get_object_or_404(BookClub, pk=pk)
    if club.memberships.count() >= club.max_members:
        return HttpResponse("Клуб заполнен", status=400)
    membership, created = ClubMembership.objects.get_or_create(
        club=club, user=request.user, defaults={"role": "member"}
    )
    if created:
        ActivityEvent.objects.create(
            user=request.user,
            event_type="join_club",
            metadata={"club_name": club.name, "club_id": club.pk},
        )
        # Добавить в чат-комнату
        from chat.models import ChatRoom, ChatParticipant
        chat_room = ChatRoom.objects.filter(room_type="club", club=club).first()
        if chat_room:
            ChatParticipant.objects.get_or_create(room=chat_room, user=request.user)
    return redirect("club_detail", pk=club.pk)


@login_required
@require_POST
def club_leave(request, pk):
    club = get_object_or_404(BookClub, pk=pk)
    ClubMembership.objects.filter(club=club, user=request.user).exclude(role="owner").delete()
    # Убрать из чата
    from chat.models import ChatRoom, ChatParticipant
    chat_room = ChatRoom.objects.filter(room_type="club", club=club).first()
    if chat_room:
        ChatParticipant.objects.filter(room=chat_room, user=request.user).delete()
    return redirect("club_detail", pk=club.pk)


@login_required
@require_GET
def club_search_books(request, pk):
    club = get_object_or_404(BookClub, pk=pk)
    membership = ClubMembership.objects.filter(club=club, user=request.user).first()
    if not membership or membership.role not in ("owner", "admin"):
        return HttpResponse(status=403)

    q = request.GET.get("q", "").strip()
    existing_ids = set(club.club_books.values_list("book_id", flat=True))

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
    return render(request, "clubs/_club_search_books.html", {"books": books, "club": club})


@login_required
@require_POST
def club_add_book(request, pk):
    club = get_object_or_404(BookClub, pk=pk)
    membership = get_object_or_404(ClubMembership, club=club, user=request.user)
    if membership.role not in ("owner", "admin"):
        return HttpResponse(status=403)

    book_id = request.POST.get("book_id")
    book = get_object_or_404(Book, pk=book_id)
    max_order = club.club_books.count()
    ClubBook.objects.get_or_create(
        club=club, book=book,
        defaults={"order": max_order},
    )
    # Return updated books list
    club_books = club.club_books.select_related("book").prefetch_related("book__authors").order_by("order")
    current_book = club_books.filter(is_current=True).first()
    oob = f'<div id="club-book-option-{book.pk}" hx-swap-oob="delete"></div>'
    from django.template.loader import render_to_string
    html = render_to_string("clubs/_club_books_list.html", {
        "club": club, "club_books": club_books, "current_book": current_book,
        "membership": membership,
    }, request=request)
    return HttpResponse(html + oob)


@login_required
@require_POST
def club_remove_book(request, pk, book_id):
    club = get_object_or_404(BookClub, pk=pk)
    membership = get_object_or_404(ClubMembership, club=club, user=request.user)
    if membership.role not in ("owner", "admin"):
        return HttpResponse(status=403)
    ClubBook.objects.filter(club=club, book_id=book_id).delete()
    club_books = club.club_books.select_related("book").prefetch_related("book__authors").order_by("order")
    current_book = club_books.filter(is_current=True).first()
    return render(request, "clubs/_club_books_list.html", {
        "club": club, "club_books": club_books, "current_book": current_book,
        "membership": membership,
    })


@login_required
@require_POST
def club_set_current_book(request, pk, book_id):
    club = get_object_or_404(BookClub, pk=pk)
    membership = get_object_or_404(ClubMembership, club=club, user=request.user)
    if membership.role not in ("owner", "admin"):
        return HttpResponse(status=403)
    club.club_books.update(is_current=False)
    ClubBook.objects.filter(club=club, book_id=book_id).update(is_current=True)
    return redirect("club_detail", pk=club.pk)


@login_required
@require_POST
def club_delete(request, pk):
    club = get_object_or_404(BookClub, pk=pk)
    membership = get_object_or_404(ClubMembership, club=club, user=request.user, role="owner")
    club.delete()
    return redirect("clubs_list")
