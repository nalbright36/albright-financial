"""
URL configuration for albright_trading_proj project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.conf import settings
from django.contrib import admin
from django.contrib.staticfiles.views import serve as serve_static
from django.urls import path, re_path

from albright_bot_api.urls import api_urlpatterns as bot_api_urlpatterns
from albright_crypto_app.views import legacy_redirect
from django.conf.urls import include
from albright_labs import password_views

urlpatterns = [
    path('admin/', admin.site.urls),
    # Password setup/reset links (Admin > Users "Send password setup link", and "Reset password" on sign-in).
    path("reset_password/", password_views.reset_request, name="password_reset"),
    path("reset_password/sent/", password_views.ResetDone.as_view(), name="password_reset_done"),
    path("reset/<uidb64>/<token>/", password_views.ResetConfirm.as_view(), name="password_reset_confirm"),
    path("reset/done/", password_views.ResetComplete.as_view(), name="password_reset_complete"),
    # Albright Labs, the hub: "/" (public landing, or the signed-in user's landing setting) and "/home/".
    path("", include("albright_labs.urls")),
    path("", include("albright_trading_app.urls")),
    path("albright_reselling_app/", include("albright_reselling_app.urls")),
    # The Opportunities app (package albright_crypto_app). Its old prefixes
    # redirect here, keeping the rest of the path and the query string.
    path("opportunities/", include("albright_crypto_app.urls")),
    # Albright Meals: the meal plan, the shared grocery list, ideas and recipes.
    path("meals/", include("albright_meals.urls")),
    re_path(r"^(?:crypto|albright_crypto_app)/(?P<rest>.*)$", legacy_redirect, name="opportunities_legacy_redirect"),
    # Bot API: the token pages (Trading nav "Bot API") and the token-authenticated JSON API.
    path("bot-api/", include("albright_bot_api.urls")),
    # The site-wide Admin area (superusers only; separate from Django's /admin/).
    # Public resume + employer chatbot (no login). Its management pages live in the Admin area.
    path("resume/", include("albright_resume.urls")),
    path("control/resume/", include("albright_resume.admin_urls")),
    path("control/labs/", include("albright_labs.admin_urls")),
    path("control/", include("albright_control.urls")),
    path("api/bot/v1/", include((bot_api_urlpatterns, "bot_api_v1"))),
]

# Local development with DEBUG off: `runserver` serves no static files, so every
# page renders unstyled (the browser refuses the 404 page as CSS - "Refused to
# apply style ... MIME type ('text/html')"). SERVE_STATIC_LOCALLY=1 in .env makes
# Django serve /static/ straight from the apps, like `runserver --insecure`,
# without turning on DEBUG. Harmless if copied to PythonAnywhere: its own
# /static/ mapping answers those requests before they ever reach Django.
if not settings.DEBUG and settings.SERVE_STATIC_LOCALLY:
    urlpatterns.append(re_path(r"^static/(?P<path>.*)$", serve_static, {"insecure": True}))
