from django.conf import settings


def globals(request):
    return {
        "recaptcha_public_key": getattr(settings, "RECAPTCHA_PUBLIC_KEY", ""),
        "telegram_bot_username": getattr(settings, "TELEGRAM_BOT_USERNAME", ""),
    }
