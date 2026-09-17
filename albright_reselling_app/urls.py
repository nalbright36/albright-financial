from django.urls import path
from . import views

app_name = "albright_reselling_app"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("ledger/", views.ledger, name="ledger"),
    path("auction-scanner/", views.auction_scanner, name="auction_scanner"),
    path("auction-scanner/<int:scan_id>/", views.scan_detail, name="scan_detail"),
    path("sleeper-segments/", views.sleeper_segments, name="sleeper_segments"),
    path("historical-data/", views.historical_data, name="historical_data"),
]