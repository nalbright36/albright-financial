from django import forms

from . import groceries
from .models import SLOTS, MealIdea, MealPlanEntry, MealsSettings, Recipe

INPUT = {"class": "ledger-input"}
DATE = {"class": "ledger-input", "type": "date"}


class PlanSlotForm(forms.Form):
    """Which day and slot: "Add to plan", "Move", and planning an idea."""
    date = forms.DateField(widget=forms.DateInput(attrs=DATE))
    slot = forms.ChoiceField(choices=SLOTS, initial="dinner", widget=forms.Select(attrs=INPUT))


class EntryForm(forms.ModelForm):
    class Meta:
        model = MealPlanEntry
        fields = ["date", "slot", "recipe", "title", "notes", "status"]
        widgets = {"date": forms.DateInput(attrs=DATE), "slot": forms.Select(attrs=INPUT),
                   "recipe": forms.Select(attrs=INPUT), "title": forms.TextInput(attrs=INPUT),
                   "notes": forms.Textarea(attrs={**INPUT, "rows": 3}), "status": forms.Select(attrs=INPUT)}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["recipe"].required = False
        self.fields["recipe"].empty_label = "No recipe - just the title"

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("recipe") and not (cleaned.get("title") or "").strip():
            self.add_error("title", "Pick a recipe or give the meal a title.")
        day, slot = cleaned.get("date"), cleaned.get("slot")
        if day and slot and MealPlanEntry.objects.filter(date=day, slot=slot).exclude(pk=self.instance.pk).exists():
            self.add_error("slot", "Something is already planned there - use Move to swap them.")
        return cleaned


class IdeaForm(forms.ModelForm):
    class Meta:
        model = MealIdea
        fields = ["title", "notes", "link"]
        widgets = {"title": forms.TextInput(attrs={**INPUT, "placeholder": "e.g. Sheet-pan gnocchi"}),
                   "notes": forms.Textarea(attrs={**INPUT, "rows": 2}),
                   "link": forms.URLInput(attrs={**INPUT, "placeholder": "https://"})}


class RecipeForm(forms.ModelForm):
    """Ingredients and steps as one per line ("2 lb chicken thighs, cut up"); tags comma-separated."""
    ingredients_text = forms.CharField(label="Ingredients (one per line)", required=False,
                                       widget=forms.Textarea(attrs={**INPUT, "rows": 8}))
    steps_text = forms.CharField(label="Steps (one per line)", required=False,
                                 widget=forms.Textarea(attrs={**INPUT, "rows": 8}))
    tags_text = forms.CharField(label="Tags (comma-separated)", required=False, widget=forms.TextInput(attrs=INPUT))

    class Meta:
        model = Recipe
        fields = ["title", "description", "prep_minutes", "cook_minutes", "servings", "source_url", "image_url",
                  "notes"]
        widgets = {"title": forms.TextInput(attrs=INPUT), "description": forms.Textarea(attrs={**INPUT, "rows": 2}),
                   "prep_minutes": forms.NumberInput(attrs=INPUT), "cook_minutes": forms.NumberInput(attrs=INPUT),
                   "servings": forms.NumberInput(attrs=INPUT), "source_url": forms.URLInput(attrs=INPUT),
                   "image_url": forms.URLInput(attrs=INPUT), "notes": forms.Textarea(attrs={**INPUT, "rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        recipe = self.instance
        if recipe.pk and not self.is_bound:
            self.fields["ingredients_text"].initial = "\n".join(ingredient_line(i) for i in recipe.ingredients)
            self.fields["steps_text"].initial = "\n".join(str(s) for s in recipe.steps)
            self.fields["tags_text"].initial = ", ".join(recipe.tags)

    def save(self, commit=True):
        recipe = super().save(commit=False)
        recipe.ingredients = [ingredient_from_line(line) for line in self.cleaned_data["ingredients_text"].splitlines()
                              if line.strip()]
        recipe.steps = [line.strip() for line in self.cleaned_data["steps_text"].splitlines() if line.strip()]
        recipe.tags = list(dict.fromkeys(t.strip().lower() for t in self.cleaned_data["tags_text"].split(",")
                                         if t.strip()))[:20]
        if commit:
            recipe.save()
        return recipe


def ingredient_from_line(line):
    parsed = groceries.parse_line(line)
    return {"name": parsed["name"], "quantity": groceries.format_quantity(parsed["quantity"]),
            "unit": parsed["unit"], "note": parsed["note"]}


def ingredient_line(ingredient):
    if isinstance(ingredient, str):
        return ingredient
    text = " ".join(str(ingredient.get(k) or "").strip() for k in ("quantity", "unit", "name")).strip()
    text = " ".join(text.split())
    return f"{text}, {ingredient['note']}" if ingredient.get("note") else text


class MealsSettingsForm(forms.ModelForm):
    class Meta:
        model = MealsSettings
        fields = ["digest_tonight"]
