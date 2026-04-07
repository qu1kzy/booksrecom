from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone
from django.db.models.signals import post_save
from django.dispatch import receiver


class UserProfile(models.Model):
    user            = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile")
    bio             = models.TextField(blank=True)
    telegram_username = models.CharField(
        max_length=100, blank=True,
        help_text="Логин Telegram без @, например: ivan_petrov"
    )
    telegram_chat_id  = models.CharField(
        max_length=50, blank=True,
        help_text="Заполняется автоматически после /start боту"
    )
    email_verified  = models.BooleanField(default=False)
    is_blocked      = models.BooleanField(default=False)
    blocked_until   = models.DateTimeField(null=True, blank=True)

    # Онбординг: показать модал при первом входе
    onboarding_done = models.BooleanField(default=False)

    # Предпочтения (онбординг + редактируются в профиле)
    favorite_genres  = models.ManyToManyField("books.Genre",  blank=True,
                                               related_name="fans")
    favorite_authors = models.ManyToManyField("books.Author", blank=True,
                                               related_name="fans")

    def __str__(self):
        return f"Profile: {self.user.username}"

    @property
    def is_currently_blocked(self):
        if not self.is_blocked:
            return False
        if self.blocked_until is None:
            return True
        return timezone.now() < self.blocked_until


class AuthorSubscription(models.Model):
    user   = models.ForeignKey(User, on_delete=models.CASCADE, related_name="author_subscriptions")
    author = models.ForeignKey("books.Author", on_delete=models.CASCADE, related_name="subscribers")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "author"], name="author_sub_unique")
        ]

    def __str__(self):
        return f"{self.user.username} → {self.author.name}"


class Achievement(models.Model):
    """Достижение пользователя (геймификация)."""

    TYPES = [
        ("books_10",       "Библиофил: 10 книг в списках"),
        ("books_50",       "Книжный червь: 50 книг в списках"),
        ("reviews_5",      "Критик: 5 отзывов"),
        ("reviews_20",     "Литературовед: 20 отзывов"),
        ("pages_1000",     "Марафонец: 1 000 страниц"),
        ("pages_5000",     "Книжный титан: 5 000 страниц"),
        ("lists_3",        "Коллекционер: 3 списка"),
        ("subscriptions_5","Фанат: 5 подписок на авторов"),
    ]

    ICONS = {
        "books_10": "📚", "books_50": "📖",
        "reviews_5": "✍️", "reviews_20": "🎓",
        "pages_1000": "🏃", "pages_5000": "🏆",
        "lists_3": "📂", "subscriptions_5": "⭐",
    }

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="achievements")
    achievement_type = models.CharField(max_length=30, choices=TYPES)
    earned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "achievement_type"],
                                    name="achievement_unique")
        ]

    def __str__(self):
        return f"{self.user.username}: {self.get_achievement_type_display()}"

    @property
    def icon(self):
        return self.ICONS.get(self.achievement_type, "🏅")


def check_achievements(user):
    """Проверить и выдать новые достижения. Вызывать после значимых действий."""
    from books.models import UserList, ReadingProgress
    from reviews.models import Review

    earned = set(user.achievements.values_list("achievement_type", flat=True))
    new = []

    book_count = (
        UserList.objects.filter(user=user)
        .values("books").distinct().count()
    )
    review_count = Review.objects.filter(user=user, status=Review.APPROVED).count()
    pages_read = (
        ReadingProgress.objects.filter(user=user)
        .aggregate(total=models.Sum("current_page"))["total"] or 0
    )
    list_count = UserList.objects.filter(user=user).count()
    sub_count = AuthorSubscription.objects.filter(user=user).count()

    checks = [
        ("books_10",        book_count >= 10),
        ("books_50",        book_count >= 50),
        ("reviews_5",       review_count >= 5),
        ("reviews_20",      review_count >= 20),
        ("pages_1000",      pages_read >= 1000),
        ("pages_5000",      pages_read >= 5000),
        ("lists_3",         list_count >= 3),
        ("subscriptions_5", sub_count >= 5),
    ]

    for atype, condition in checks:
        if atype not in earned and condition:
            Achievement.objects.create(user=user, achievement_type=atype)
            new.append(atype)

    return new


@receiver(post_save, sender=User)
def create_user_defaults(sender, instance, created, **kwargs):
    if not created:
        return
    UserProfile.objects.get_or_create(user=instance)
    from books.models import UserList
    UserList.objects.get_or_create(
        user=instance,
        name="Избранное",
        defaults={"is_default": True, "sentiment_tag": "positive"},
    )


@receiver(post_save, sender="books.UserList")
def classify_new_list(sender, instance, created, **kwargs):
    """Classify sentiment of new list via Claude."""
    if not created or instance.is_default:
        return
    from users.tasks import classify_list_sentiment
    classify_list_sentiment.delay(instance.pk)
