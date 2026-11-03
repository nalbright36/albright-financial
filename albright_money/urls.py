from django.urls import path

from . import views

app_name = "albright_money"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("transactions/", views.transactions, name="transactions"),
    path("transactions/add/", views.quick_add, name="quick_add"),
    path("transactions/<int:pk>/", views.transaction, name="transaction"),
    path("transactions/<int:pk>/delete/", views.transaction_delete, name="transaction_delete"),
    path("transactions/<int:pk>/category/", views.set_category, name="set_category"),
    path("transactions/<int:pk>/always/", views.always, name="always"),
    path("transfer/", views.transfer, name="transfer"),
    path("accounts/", views.accounts, name="accounts"),
    path("accounts/<int:pk>/", views.account, name="account"),
    path("import/", views.import_csv, name="import"),
    path("categories/", views.categories, name="categories"),
    path("categories/<int:pk>/", views.category, name="category"),
    path("rules/", views.rules, name="rules"),
    path("rules/<int:pk>/delete/", views.rule_delete, name="rule_delete"),
    path("budget/", views.budget, name="budget"),
    path("household/", views.household, name="household"),
]
