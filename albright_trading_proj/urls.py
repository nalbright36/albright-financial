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
    path("strategies/<int:strategy_id>/", views.strategy_detail, name="strategy_detail"),
    path("strategies/archived/", views.archived_strategies, name="archived_strategies"),
    path("strategies/<int:strategy_id>/archive/", views.strategy_archive, name="strategy_archive"),
    path("strategies/<int:strategy_id>/unarchive/", views.strategy_unarchive, name="strategy_unarchive"),
    path("strategies/options/", views.option_strategies, name="option_strategies"),
    path("strategies/options/new/", views.option_strategy_create, name="option_strategy_create"),
    path("strategies/options/archived/", views.option_archived_strategies, name="option_archived_strategies"),
    path("strategies/options/<int:strategy_id>/", views.option_strategy_detail, name="option_strategy_detail"),
    path("strategies/options/<int:strategy_id>/toggle/", views.option_strategy_toggle, name="option_strategy_toggle"),
    path("strategies/options/<int:strategy_id>/archive/", views.option_strategy_archive, name="option_strategy_archive"),
    path("strategies/options/<int:strategy_id>/unarchive/", views.option_strategy_unarchive, name="option_strategy_unarchive"),
    path("strategies/<int:strategy_id>/close-all/", views.close_all_stock_positions, name="close_all_stock_positions"),
    path("trades/<int:trade_id>/close/", views.close_stock_position, name="close_stock_position"),
    path("strategies/options/<int:strategy_id>/close-all/", views.close_all_option_positions, name="close_all_option_positions"),
    path("trades/options/<int:trade_id>/close/", views.close_option_position, name="close_option_position"),
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
