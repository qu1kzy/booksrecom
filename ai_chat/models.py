from django.db import models
from django.conf import settings


class BookChat(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="book_chats",
    )
    book = models.ForeignKey(
        "books.Book", on_delete=models.CASCADE, related_name="ai_chats"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("user", "book")

    def __str__(self):
        return f"{self.user} — {self.book}"


class BookChatMessage(models.Model):
    ROLE_CHOICES = [("user", "Пользователь"), ("assistant", "AI")]

    chat = models.ForeignKey(
        BookChat, on_delete=models.CASCADE, related_name="messages"
    )
    role = models.CharField(max_length=10, choices=ROLE_CHOICES)
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.role}: {self.content[:50]}"


class DiscoveryChat(models.Model):
    """AI-чат для поиска книг (не привязан к конкретной книге)."""
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="discovery_chats",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return f"Discovery: {self.user} ({self.created_at})"


class DiscoveryChatMessage(models.Model):
    ROLE_CHOICES = [("user", "Пользователь"), ("assistant", "AI")]

    chat = models.ForeignKey(
        DiscoveryChat, on_delete=models.CASCADE, related_name="messages"
    )
    role = models.CharField(max_length=10, choices=ROLE_CHOICES)
    content = models.TextField()
    recommended_books = models.ManyToManyField("books.Book", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"[Discovery] {self.role}: {self.content[:50]}"


class BookContent(models.Model):
    book = models.OneToOneField(
        "books.Book", on_delete=models.CASCADE, related_name="ai_content"
    )
    content_text = models.TextField(
        help_text="Расширенное описание, краткое содержание, ключевые цитаты"
    )
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Контент: {self.book.title}"
