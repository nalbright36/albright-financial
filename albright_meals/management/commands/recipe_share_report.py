"""Read-only report: where a recipe lives and who it is shared with (person or circle grants).

Does not change any data. Use it to check a recipe such as "Polish Kolaczki" after a circle share:
  python manage.py recipe_share_report --title "Polish Kolaczki"

Sharing a recipe with a circle adds a ShareGrant; it does not copy the recipe or move its space.
Members see it on All recipes and on that circle's filter after the list fix, as long as the grant exists
and they have Meals opted in for the circle. If the grant is missing, share it again (or use Move to space
to put the recipe in the circle's space).
"""
from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand

from albright_labs.models import ShareGrant
from albright_meals.models import Recipe


class Command(BaseCommand):
    help = "Show a recipe's space and share grants (read-only). Does not change data."

    def add_arguments(self, parser):
        parser.add_argument("--title", default="", help="Case-insensitive title substring (all recipes if omitted).")

    def handle(self, *args, **options):
        qs = Recipe.objects.select_related("space__circle", "space__user", "created_by").order_by("title", "pk")
        title = (options.get("title") or "").strip()
        if title:
            qs = qs.filter(title__icontains=title)
        ct = ContentType.objects.get_for_model(Recipe)
        grants = list(ShareGrant.objects.filter(content_type=ct, object_id__in=qs.values("pk"))
                      .select_related("user", "circle", "created_by"))
        by_recipe = {}
        for g in grants:
            by_recipe.setdefault(g.object_id, []).append(g)
        if not qs.exists():
            self.stdout.write("No recipes matched.")
            return
        for recipe in qs:
            space = "Personal" if recipe.space.user_id else recipe.space.circle.name
            owner = (recipe.space.user.get_username() if recipe.space.user_id else "circle")
            self.stdout.write(f"{recipe.title}  pk={recipe.pk}  space={space} ({owner})  created_by="
                              f"{recipe.created_by.get_username() if recipe.created_by_id else '?'}")
            rows = by_recipe.get(recipe.pk) or []
            if not rows:
                self.stdout.write("  no share grants")
                continue
            for g in rows:
                target = f"circle:{g.circle.name}" if g.circle_id else f"person:{g.user.get_username()}"
                hidden = " hidden" if g.hidden_at else ""
                self.stdout.write(f"  grant {g.permission} -> {target}  by="
                                  f"{g.created_by.get_username() if g.created_by_id else '?'}{hidden}")
