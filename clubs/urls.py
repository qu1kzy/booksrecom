from django.urls import path
from . import views

urlpatterns = [
    path("", views.clubs_list, name="clubs_list"),
    path("create/", views.club_create, name="club_create"),
    path("<int:pk>/", views.club_detail, name="club_detail"),
    path("<int:pk>/join/", views.club_join, name="club_join"),
    path("<int:pk>/leave/", views.club_leave, name="club_leave"),
    path("<int:pk>/delete/", views.club_delete, name="club_delete"),
    path("<int:pk>/add-book/", views.club_add_book, name="club_add_book"),
    path("<int:pk>/search-books/", views.club_search_books, name="club_search_books"),
    path("<int:pk>/remove-book/<int:book_id>/", views.club_remove_book, name="club_remove_book"),
    path("<int:pk>/set-current/<int:book_id>/", views.club_set_current_book, name="club_set_current_book"),
]
