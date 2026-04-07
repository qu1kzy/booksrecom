from django.db import models
from django.conf import settings


class BookClub(models.Model):
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    cover_image = models.ImageField(upload_to="clubs/", null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="clubs_created",
    )
    is_public = models.BooleanField(default=True)
    max_members = models.PositiveIntegerField(default=50)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.name

    def member_count(self):
        return self.memberships.count()


class ClubMembership(models.Model):
    ROLE_CHOICES = [
        ("owner", "Владелец"),
        ("admin", "Администратор"),
        ("member", "Участник"),
    ]
    club = models.ForeignKey(
        BookClub, on_delete=models.CASCADE, related_name="memberships"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="club_memberships",
    )
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default="member")
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("club", "user")

    def __str__(self):
        return f"{self.user} in {self.club} ({self.role})"


class ClubBook(models.Model):
    club = models.ForeignKey(
        BookClub, on_delete=models.CASCADE, related_name="club_books"
    )
    book = models.ForeignKey(
        "books.Book", on_delete=models.CASCADE, related_name="in_clubs"
    )
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    is_current = models.BooleanField(default=False)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = ("club", "book")
        ordering = ["order"]

    def __str__(self):
        return f"{self.club.name}: {self.book.title}"
