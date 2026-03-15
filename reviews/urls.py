from django.urls import path
from . import views

urlpatterns = [
    path("<int:book_id>/create/",     views.review_create,   name="review_create"),
    path("<int:review_id>/moderate/", views.review_moderate, name="review_moderate"),
]
