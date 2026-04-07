from django.contrib.auth.models import User
from django.db import models


class Genre(models.Model):
    """Жанр книги (детектив, фэнтези и т.д.)."""

    name = models.CharField(max_length=100, unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Author(models.Model):
    """Автор книги с краткой биографией и годом рождения."""

    name = models.CharField(max_length=250)
    bio = models.TextField(blank=True)
    birth_year = models.IntegerField(null=True, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Publisher(models.Model):
    """Издательство, выпускающее книги."""

    name = models.CharField(max_length=250, unique=True)

    def __str__(self) -> str:
        return self.name


class Series(models.Model):
    """Книжная серия, к которой может относиться книга."""

    name = models.CharField(max_length=250)

    def __str__(self) -> str:
        return self.name


class Language(models.Model):
    """Язык оригинала или издания книги."""

    name = models.CharField(max_length=100, unique=True)

    def __str__(self) -> str:
        return self.name


class Book(models.Model):
    """Книга в каталоге с денормализованными полями рейтинга и цены."""

    title = models.CharField(max_length=250, db_index=True)
    isbn = models.CharField(max_length=20, unique=True, blank=True, null=True)
    description = models.TextField(blank=True)
    publication_year = models.IntegerField(db_index=True, null=True, blank=True)
    pages = models.PositiveIntegerField(null=True, blank=True)
    avg_rating = models.FloatField(default=0.0, db_index=True)
    rating_count = models.PositiveIntegerField(default=0)
    cover_image = models.ImageField(upload_to="covers/", blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    avg_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    price_last_requested = models.DateTimeField(null=True, blank=True)

    authors = models.ManyToManyField(Author, blank=True, related_name="books")
    genres = models.ManyToManyField(Genre, blank=True, related_name="books")
    publisher = models.ForeignKey(
        Publisher,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="books",
    )
    series = models.ForeignKey(
        Series,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="books",
    )
    language = models.ForeignKey(
        Language,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="books",
    )

    class Meta:
        ordering = ["-avg_rating"]

    def __str__(self) -> str:
        return self.title

    @property
    def stars_display(self) -> str:
        """Представление среднего рейтинга в виде пятизвёздочной строки."""
        r = round(self.avg_rating)
        return "★" * r + "☆" * (5 - r)


class UserList(models.Model):
    SENTIMENT_CHOICES = [
        ("positive", "Нравится"),
        ("negative", "Не нравится"),
        ("neutral", "Нейтральный"),
        ("wishlist", "Хочу прочитать"),
    ]
    """
    Пользовательский список книг.

    sentiment_tag задаёт эмоциональную окраску списка
    и влияет на качество персональных рекомендаций.
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="book_lists")
    name = models.CharField(max_length=100)
    is_default = models.BooleanField(default=False)
    is_public = models.BooleanField(default=False)
    sentiment_tag = models.CharField(
        max_length=20,
        default="neutral",
        choices=SENTIMENT_CHOICES,
        db_index=True,
    )
    books = models.ManyToManyField(Book, blank=True, related_name="in_lists")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-is_default", "name"]
        constraints = [
            models.UniqueConstraint(fields=["user", "name"], name="userlist_unique_name")
        ]

    def __str__(self) -> str:
        return f"{self.user.username} / {self.name}"


class Store(models.Model):
    """Онлайн-магазин, из которого парсятся цены на книги."""

    name = models.CharField(max_length=250)
    base_url = models.URLField()
    icon = models.CharField(max_length=10, blank=True)
    price_selector = models.CharField(
        max_length=500,
        blank=True,
        help_text="CSS-селектор цены (например: .price)",
    )
    is_active = models.BooleanField(default=True)

    def __str__(self) -> str:
        return self.name


class BookStore(models.Model):
    """Связь книги с магазином и текущей ценой в этом магазине."""

    book = models.ForeignKey(Book, on_delete=models.CASCADE, related_name="store_links")
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="book_links")
    product_url = models.URLField(max_length=500)
    current_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    in_stock = models.BooleanField(default=True)
    last_checked = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["book", "store"], name="bookstore_unique")
        ]

    def __str__(self) -> str:
        return f"{self.book.title} @ {self.store.name}"


class BookPrice(models.Model):
    """История цен: одна запись = одна проверка цены в одном магазине."""
    book_store = models.ForeignKey(BookStore, on_delete=models.CASCADE, related_name="price_history")
    price = models.DecimalField(max_digits=10, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.price} ({self.book_store}) от: {self.created_at}"


class BookTag(models.Model):
    """
    Тег книги, извлечённый Claude из одобренных отзывов.
    Глобальный пул: одно слово/фраза может встречаться у разных книг.
    count — сколько отзывов дали этот тег для этой книги.
    """
    book = models.ForeignKey(Book, on_delete=models.CASCADE, related_name="tags")
    name = models.CharField(max_length=80)
    count = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["-count"]
        constraints = [
            models.UniqueConstraint(fields=["book", "name"], name="booktag_unique")
        ]

    def __str__(self):
        return f"{self.name} ({self.book.title}, ×{self.count})"


class ReadingProgress(models.Model):
    """Прогресс чтения: текущая страница пользователя в книге."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="reading_progress")
    book = models.ForeignKey(Book, on_delete=models.CASCADE, related_name="reading_progress")
    current_page = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        constraints = [
            models.UniqueConstraint(fields=["user", "book"], name="readingprogress_unique")
        ]

    def percent(self):
        if self.book.pages and self.book.pages > 0:
            return min(100, round(self.current_page / self.book.pages * 100))
        return 0

    def __str__(self):
        return f"{self.user.username} — {self.book.title}: {self.current_page}"


class Quote(models.Model):
    """Цитата из книги, сохранённая пользователем или сгенерированная AI."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="quotes")
    book = models.ForeignKey(Book, on_delete=models.CASCADE, related_name="quotes")
    text = models.TextField()
    page_number = models.PositiveIntegerField(null=True, blank=True)
    is_ai_generated = models.BooleanField(default=False)
    mood_tag = models.ForeignKey(
        "MoodTag", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="quotes",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"«{self.text[:40]}» — {self.user.username}"


class PriceAlert(models.Model):
    """Уведомление когда цена книги упадёт ниже порога."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="price_alerts")
    book = models.ForeignKey(Book, on_delete=models.CASCADE, related_name="price_alerts")
    threshold = models.DecimalField(max_digits=10, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)
    triggered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "book"], name="pricealert_unique")
        ]

    def __str__(self):
        return f"{self.user.username} alert {self.book.title} < {self.threshold}₽"


class MoodTag(models.Model):
    """Структурированный тег настроения/атмосферы книги."""
    CATEGORY_CHOICES = [
        ("atmosphere", "Атмосфера"),
        ("pace", "Темп"),
        ("emotion", "Эмоция"),
        ("complexity", "Сложность"),
    ]
    name = models.CharField(max_length=50, unique=True)
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES)
    icon = models.CharField(max_length=10, blank=True)

    class Meta:
        ordering = ["category", "name"]

    def __str__(self):
        return f"{self.icon} {self.name}" if self.icon else self.name


class BookMood(models.Model):
    """Связь книги с mood-тегом (AI-классификация + пользовательские голоса)."""
    book = models.ForeignKey(Book, on_delete=models.CASCADE, related_name="moods")
    mood = models.ForeignKey(MoodTag, on_delete=models.CASCADE, related_name="book_moods")
    confidence = models.FloatField(default=1.0)
    source = models.CharField(max_length=20, default="ai")  # ai / user_vote
    vote_count = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["book", "mood"], name="bookmood_unique")
        ]

    def __str__(self):
        return f"{self.book.title} — {self.mood.name}"
