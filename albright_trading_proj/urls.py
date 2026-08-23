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
from django.urls import path
from django.conf.urls import include
from albright_trading_app import views

urlpatterns = [
    path('admin/', admin.site.urls),
    path('',views.home,name='home'),
    path('test/', views.test, name='test'),
    path('strategies/', views.strategies, name='strategies'),
    path("strategies/new/", views.strategy_create, name="strategy_create"),
    path("strategies/<int:strategy_id>/toggle/", views.strategy_toggle, name="strategy_toggle"),
    path('user_login/', views.user_login, name='user_login'),
    path('user_logout/', views.user_logout, name='user_logout'),
    path('registration/', views.registration, name='registration'),
    path('registrationsuccess/', views.registrationsuccess, name='registrationsuccess'),
    path('trading_account/', views.trading_account, name='trading_account'),
    path('market_scanner/', views.market_scanner, name='market_scanner'),
    path("stock/<str:symbol>/", views.stock_detail, name="stock_detail"),
    path("stock/<str:symbol>/bars/", views.stock_bars_api, name="stock_bars_api"),
    path("account/connect-alpaca/", views.connect_alpaca_account, name="connect_alpaca_account"),
    path("account/disconnect-alpaca/", views.disconnect_alpaca_account, name="disconnect_alpaca_account"),
]
