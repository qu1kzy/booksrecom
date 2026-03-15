from django.shortcuts import get_object_or_404
from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import HttpResponse
from django.template.response import TemplateResponse
from .models import Review
from books.models import Book
from .tasks import extract_tag_for_review
from books.tag_extraction import decrement_tag_from_review


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
        # Запускаем извлечение тега асинхронно

        extract_tag_for_review.delay(review.pk)

    elif action == "reject":

        # Декрементируем тег если он уже был извлечён (повторное модерирование)
        if review.extracted_tag:
            review._extracted_tag = review.extracted_tag
            decrement_tag_from_review(review)
        review.delete()

    return HttpResponse("")
