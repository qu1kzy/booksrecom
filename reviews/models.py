from django.contrib.auth.models import User
from django.db import models
from django.core.validators import MinValueValidator, MaxValueValidator


class Review(models.Model):
    PENDING  = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    STATUS_CHOICES = [(PENDING,"На модерации"),(APPROVED,"Одобрен"),(REJECTED,"Отклонён")]

    user       = models.ForeignKey(User, on_delete=models.CASCADE, related_name="reviews")
    book       = models.ForeignKey("books.Book", on_delete=models.CASCADE, related_name="reviews")
    rating     = models.IntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    text       = models.TextField(blank=True)
    status     = models.CharField(max_length=10, choices=STATUS_CHOICES, default=PENDING)
    created_at    = models.DateTimeField(auto_now_add=True)
    extracted_tag = models.CharField(max_length=80, blank=True, default="",
                    help_text="Тег, извлечённый Claude из этого отзыва")

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["user", "book"], name="review_unique")
        ]

    @property
    def stars_display(self):
        return "★" * self.rating + "☆" * (5 - self.rating)

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        _recalc(self.book)

    def delete(self, *args, **kwargs):
        book = self.book
        super().delete(*args, **kwargs)
        _recalc(book)


def _recalc(book):
    from django.db.models import Avg, Count
    stats = Review.objects.filter(book=book, status=Review.APPROVED).aggregate(
        avg=Avg("rating"), cnt=Count("id")
    )
    book.avg_rating   = round(stats["avg"] or 0.0, 2)
    book.rating_count = stats["cnt"] or 0
    book.save(update_fields=["avg_rating", "rating_count"])
