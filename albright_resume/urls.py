from django.urls import path

from . import views

app_name = "albright_resume"

urlpatterns = [
    path("", views.resume_page, name="page"),
    path("resume.pdf", views.resume_pdf, name="pdf"),
    path("shots/<int:pk>/<int:version>/", views.project_screenshot, name="screenshot"),
    path("chat/", views.chat_api, name="chat"),
]
