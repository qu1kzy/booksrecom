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
