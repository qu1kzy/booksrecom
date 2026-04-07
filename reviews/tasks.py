import logging
from celery import shared_task
from reviews.models import Review
from books.tag_extraction import extract_tag_from_review, apply_tag_to_book

logger = logging.getLogger(__name__)


@shared_task
def extract_tag_for_review(review_id: int) -> None:
    """
    Celery-задача: вызывает Claude для извлечения тега из одобренного отзыва
    и применяет результат к книге.
    """


    try:
        review = Review.objects.select_related("book").prefetch_related("book__authors").get(pk=review_id)
    except Review.DoesNotExist:
        logger.warning("extract_tag_for_review: review #%d not found", review_id)
        return

    tag = extract_tag_from_review(review)
    if not tag:
        return

    apply_tag_to_book(review.book, tag)

    # Сохраняем тег в отзыве для возможного декремента при отклонении
    review.extracted_tag = tag
    review.save(update_fields=["extracted_tag"])
    logger.info("Tag '%s' applied to book #%d from review #%d", tag, review.book.pk, review_id)
