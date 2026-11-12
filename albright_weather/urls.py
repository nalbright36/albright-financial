from django.urls import path

from . import views

app_name = "albright_weather"

urlpatterns = [
    path("", views.home, name="home"),
    path("places/add/", views.place_add, name="place_add"),
    path("places/<int:pk>/remove/", views.place_remove, name="place_remove"),
]
