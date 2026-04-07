from django.shortcuts import get_object_or_404
from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import HttpResponse
from django.template.response import TemplateResponse
from django.views.decorators.http import require_POST
from .models import Review, ReviewLike
from books.models import Book
from .tasks import extract_tag_for_review
from books.tag_extraction import decrement_tag_from_review


def _notify_review_status(review, approved: bool) -> None:
    """Уведомить автора отзыва: Telegram → email fallback."""
    profile = getattr(review.user, "profile", None)
    book = review.book

    # Telegram (приоритет)
    if profile and profile.telegram_chat_id:
        from notifications.telegram import send_message
        from django.conf import settings as conf
        book_url = f"{getattr(conf, 'SITE_URL', '')}/books/{book.pk}/"
        if approved:
            text = (
                f"✅ <b>Ваш отзыв одобрен</b>\n\n"
                f"Книга: <b>{book.title}</b>\n"
                f"<a href='{book_url}'>Открыть</a>"
            )
        else:
            text = (
                f"❌ <b>Ваш отзыв отклонён</b>\n\n"
                f"Книга: <b>{book.title}</b>"
            )
        send_message(profile.telegram_chat_id, text)
        return

    # Email fallback
    from notifications.email import send_review_status_email
    send_review_status_email(review.user, book, approved)


@login_required
def review_create(request, book_id):
    book = get_object_or_404(Book, pk=book_id)
    if request.method != "POST":
        return HttpResponse(status=405)

    rating = int(request.POST.get("rating", 0))
    text   = request.POST.get("text", "").strip()

    if not (1 <= rating <= 5) or not text:
        return TemplateResponse(request, "reviews/_review_form.html",
                                {"book": book, "error": "Укажите оценку и текст."})

    Review.objects.get_or_create(
        user=request.user, book=book,
        defaults={"rating": rating, "text": text},
    )
    return TemplateResponse(request, "reviews/_review_done.html")


@user_passes_test(lambda u: u.is_staff)
def review_moderate(request, review_id):
    review = get_object_or_404(Review, pk=review_id)
    action = request.POST.get("action")

    if action == "approve":
        review.status = Review.APPROVED
        review.save(update_fields=["status"])
        extract_tag_for_review.delay(review.pk)
        _notify_review_status(review, approved=True)

    elif action == "reject":
        _notify_review_status(review, approved=False)
        # Декрементируем тег если он уже был извлечён (повторное модерирование)
        if review.extracted_tag:
            review._extracted_tag = review.extracted_tag
            decrement_tag_from_review(review)
        review.delete()

    return HttpResponse("")


def reviews_page(request, book_id):
    """HTMX: пагинация отзывов книги (по 5 штук)."""
    from django.db.models import Count, Exists, OuterRef
    REVIEWS_PER_PAGE = 5
    book = get_object_or_404(Book, pk=book_id)
    page = int(request.GET.get("page", 1))
    offset = (page - 1) * REVIEWS_PER_PAGE

    _like_filter = (
        ReviewLike.objects.filter(review=OuterRef("pk"), user=request.user)
        if request.user.is_authenticated
        else ReviewLike.objects.none()
    )
    qs = (
        Review.objects
        .filter(book=book, status=Review.APPROVED)
        .select_related("user")
        .annotate(
            likes_count=Count("likes", distinct=True),
            user_liked=Exists(_like_filter),
        )
        .order_by("-likes_count", "-created_at")
    )
    total = qs.count()
    reviews = qs[offset:offset + REVIEWS_PER_PAGE]
    has_more = (offset + REVIEWS_PER_PAGE) < total

    return TemplateResponse(request, "reviews/_review_list.html", {
        "reviews": reviews,
        "book": book,
        "has_more_reviews": has_more,
        "next_page": page + 1,
    })


@login_required
@require_POST
def review_like(request, review_id):
    """HTMX: поставить / снять лайк на одобренном отзыве."""
    review = get_object_or_404(Review, pk=review_id, status=Review.APPROVED)
    like, created = ReviewLike.objects.get_or_create(user=request.user, review=review)
    if not created:
        like.delete()
        liked = False
    else:
        liked = True
    likes_count = review.likes.count()
    return TemplateResponse(request, "reviews/_like_btn.html", {
        "review": review, "liked": liked, "likes_count": likes_count,
    })
