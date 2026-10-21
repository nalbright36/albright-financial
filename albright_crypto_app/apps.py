from django.apps import AppConfig


class AlbrightCryptoAppConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'albright_crypto_app'   # package/label kept so migrations and data stay put
    verbose_name = "Opportunities"
