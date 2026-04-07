from django.urls import path
from . import views

urlpatterns = [
    path("<int:book_id>/", views.book_chat, name="book_chat"),
    path("<int:book_id>/send/", views.book_chat_send, name="book_chat_send"),
    path("<int:book_id>/clear/", views.book_chat_clear, name="book_chat_clear"),
    path("discovery/", views.discovery_chat, name="discovery_chat"),
    path("discovery/send/", views.discovery_send, name="discovery_send"),
    path("discovery/clear/", views.discovery_clear, name="discovery_clear"),
]
