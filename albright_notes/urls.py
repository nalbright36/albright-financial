from django.urls import path

from . import views

app_name = "albright_notes"

urlpatterns = [
    path("", views.note_list, name="list"),
    path("new/", views.note_new, name="new"),
    path("capture/", views.capture, name="capture"),
    path("links/search/", views.link_search, name="link_search"),
    path("leave/", views.leave, name="leave"),
    path("<int:pk>/", views.note_page, name="note"),
    path("<int:pk>/save/", views.note_save, name="save"),
    path("<int:pk>/check/", views.note_check, name="check"),
    path("<int:pk>/pin/", views.note_pin, name="pin"),
    path("<int:pk>/archive/", views.note_archive, name="archive"),
    path("<int:pk>/delete/", views.note_delete, name="delete"),
    path("<int:pk>/share/", views.share_add, name="share_add"),
    path("<int:pk>/share/<int:share_id>/", views.share_update, name="share_update"),
    path("<int:pk>/links/", views.link_add, name="link_add"),
    path("<int:pk>/links/<int:link_id>/remove/", views.link_remove, name="link_remove"),
]
