from django.urls import path

from albright_labs.items import item_page

from . import views

app_name = "albright_notes"

urlpatterns = [
    path("", views.note_list, name="list"),
    path("new/", views.note_new, name="new"),
    path("capture/", views.capture, name="capture"),
    path("links/search/", views.link_search, name="link_search"),
    path("leave/", views.leave, name="leave"),
    path("settings/", views.smart_settings, name="settings"),
    path("<int:pk>/", item_page(views.note_page), name="note"),
    path("<int:pk>/save/", views.note_save, name="save"),
    path("<int:pk>/check/", views.note_check, name="check"),
    path("<int:pk>/pin/", views.note_pin, name="pin"),
    path("<int:pk>/archive/", views.note_archive, name="archive"),
    path("<int:pk>/delete/", views.note_delete, name="delete"),
    path("<int:pk>/restore/", views.note_restore, name="restore"),
    path("<int:pk>/purge/", views.note_purge, name="purge"),
    path("<int:pk>/links/", views.link_add, name="link_add"),
    path("<int:pk>/groceries/", views.smart_groceries, name="groceries"),
    path("<int:pk>/groceries/remove/", views.smart_groceries_remove, name="groceries_remove"),
    path("<int:pk>/budget/", views.smart_budget, name="budget"),
    path("<int:pk>/budget/remove/", views.smart_budget_remove, name="budget_remove"),
    path("<int:pk>/budget/<int:draft_id>/", views.smart_budget_info, name="budget_info"),
    path("<int:pk>/planner/", views.smart_planner, name="planner"),
    path("<int:pk>/planner/<int:task_id>/delete/", views.smart_planner_delete, name="planner_delete"),
    path("<int:pk>/state/", views.smart_state, name="state"),
    path("<int:pk>/links/<int:link_id>/remove/", views.link_remove, name="link_remove"),
]
