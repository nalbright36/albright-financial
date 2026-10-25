"""Albright Labs - the site hub at "/": a public landing page for visitors
and, once signed in, Home (a tile per app) plus where to land after signing in."""
from django.conf import settings
from django.db import models

LANDING_CHOICES = [("home", "Home"), ("last_app", "Last used app"), ("page", "A specific page")]


class UserPreference(models.Model):
    """Per-user settings for the hub. landing_page is a key from
    landing.LANDING_PAGES (e.g. "resell:watchlist")."""
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="labs_preference")
    landing = models.CharField(max_length=10, choices=LANDING_CHOICES, default="home")
    landing_page = models.CharField(max_length=40, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user}: {self.landing}{' ' + self.landing_page if self.landing_page else ''}"

    @classmethod
    def for_user(cls, user):
        """The saved preference, or an unsaved default (Home) - never creates a row just by reading."""
        return cls.objects.filter(user=user).first() or cls(user=user)


class LandingSettings(models.Model):
    """The public landing page's editable text (Admin > Home page). One row."""
    SINGLETON_PK = 1
    name = models.CharField(max_length=120, blank=True,
                            help_text="Shown as the hero title. Blank: the name on the resume page.")
    headline = models.CharField(max_length=200, blank=True, help_text="One line under the name.")
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return "Landing page settings"

    @classmethod
    def load(cls):
        return cls.objects.get_or_create(pk=cls.SINGLETON_PK)[0]
