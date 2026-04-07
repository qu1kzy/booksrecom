from django.urls import path
from . import views

urlpatterns = [
    path("<int:book_id>/create/",     views.review_create,   name="review_create"),
    path("<int:book_id>/page/",       views.reviews_page,    name="reviews_page"),
    path("<int:review_id>/moderate/", views.review_moderate, name="review_moderate"),
    path("<int:review_id>/like/",     views.review_like,     name="review_like"),
]
