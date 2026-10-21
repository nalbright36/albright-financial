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
from django.contrib import admin
from django.urls import path, re_path

from albright_crypto_app.views import legacy_redirect
from django.conf.urls import include

urlpatterns = [
    path('admin/', admin.site.urls),
    path("", include("albright_trading_app.urls")),
    path("albright_reselling_app/", include("albright_reselling_app.urls")),
    # The Opportunities app (package albright_crypto_app). Its old prefixes
    # redirect here, keeping the rest of the path and the query string.
    path("opportunities/", include("albright_crypto_app.urls")),
    re_path(r"^(?:crypto|albright_crypto_app)/(?P<rest>.*)$", legacy_redirect, name="opportunities_legacy_redirect"),
]
