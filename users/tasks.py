import logging
from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=1, default_retry_delay=10)
def generate_ai_recommendations_task(self, user_id: int):
    """
    Celery-задача: запускает AI-рекомендации для пользователя и кеширует результат.
    Запускается по нажатию кнопки «Обновить рекомендации» в профиле.
    """
    from django.contrib.auth.models import User
    from books.ai_recommendations import generate_ai_recommendations

    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        logger.warning("generate_ai_recommendations_task: user %d not found", user_id)
        return {"status": "error", "message": "Пользователь не найден"}

    try:
        result = generate_ai_recommendations(user)
        logger.info("AI recs for user %d: %d books", user_id, len(result))
        return {"status": "ok", "count": len(result)}
    except Exception as exc:
        logger.error("AI recs task failed for user %d: %s", user_id, exc)
        try:
            raise self.retry(exc=exc)
        except self.MaxRetriesExceededError:
            return {"status": "error", "message": str(exc)}


@shared_task
def classify_list_sentiment(list_id: int):
    """Определить тональность списка через Claude и сохранить sentiment_tag."""
    from django.conf import settings as conf
    if not getattr(conf, "ANTHROPIC_API_KEY", ""):
        return

    from books.models import UserList
    try:
        ul = UserList.objects.get(pk=list_id)
    except UserList.DoesNotExist:
        return

    import anthropic
    prompt = (
        f"Пользователь создал список книг с названием: «{ul.name}»\n\n"
        "Определи тональность этого списка. Ответь ТОЛЬКО одним словом:\n"
        "- positive  (нравится, избранное, лучшее, топ, любимое)\n"
        "- negative  (не нравится, разочарования, плохое, мусор, бросил)\n"
        "- wishlist  (хочу прочитать, буду читать, план, очередь, to-read)\n"
        "- neutral   (всё остальное: прочитано, читаю, архив и т.п.)"
    )

    try:
        client = anthropic.Anthropic(api_key=conf.ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            messages=[{"role": "user", "content": prompt}],
        )
        tag = msg.content[0].text.strip().lower()
        if tag in ("positive", "negative", "wishlist", "neutral"):
            ul.sentiment_tag = tag
            ul.save(update_fields=["sentiment_tag"])
            logger.info("List #%d «%s» → sentiment: %s", list_id, ul.name, tag)
        else:
            logger.warning("Unexpected sentiment tag '%s' for list #%d", tag, list_id)
    except Exception as exc:
        logger.error("classify_list_sentiment error for list #%d: %s", list_id, exc)
