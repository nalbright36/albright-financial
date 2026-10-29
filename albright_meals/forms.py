from django import forms

from . import groceries, importer
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
    steps_text = forms.CharField(label='Steps (one per line; "## Sauce" starts a section)', required=False,
                                 widget=forms.Textarea(attrs={**INPUT, "rows": 8}))
    tags_text = forms.CharField(label="Tags (comma-separated)", required=False, widget=forms.TextInput(attrs=INPUT))

    class Meta:
        model = Recipe
        fields = ["title", "description", "prep_minutes", "cook_minutes", "total_time_minutes", "servings",
                  "source_url", "image_url", "notes", "needs_review"]
        labels = {"total_time_minutes": "Total minutes", "needs_review": "Needs review (read by the AI - "
                                                                          "untick once you've checked it)"}
        help_texts = {"total_time_minutes": "", "needs_review": ""}
        widgets = {"title": forms.TextInput(attrs=INPUT), "description": forms.Textarea(attrs={**INPUT, "rows": 2}),
                   "prep_minutes": forms.NumberInput(attrs=INPUT), "cook_minutes": forms.NumberInput(attrs=INPUT),
                   "total_time_minutes": forms.NumberInput(attrs=INPUT),
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
        # An imported line left as it was keeps its parts and its original text.
        unchanged = {ingredient_line(i): i for i in self.instance.ingredients or [] if isinstance(i, dict)}
        recipe = super().save(commit=False)
        recipe.ingredients = [unchanged.get(line.strip()) or ingredient_from_line(line)
                              for line in self.cleaned_data["ingredients_text"].splitlines() if line.strip()]
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
    if ingredient.get("original"):
        return ingredient["original"]
    text = " ".join(str(ingredient.get(k) or "").strip() for k in ("quantity", "unit", "name")).strip()
    text = " ".join(text.split())
    return f"{text}, {ingredient['note']}" if ingredient.get("note") else text


class ImportLinkForm(forms.Form):
    url = forms.URLField(max_length=importer.MAX_URL, assume_scheme="https")


class ImportPreviewForm(forms.Form):
    """The import preview (Recipes > Import from link): what was read from the page, to fix before saving."""
    MINUTES = {"required": False, "min_value": 0, "max_value": importer.MAX_MINUTES,
               "widget": forms.NumberInput(attrs={**INPUT, "inputmode": "numeric"})}
    import_id = forms.CharField(max_length=40, widget=forms.HiddenInput)
    title = forms.CharField(max_length=200, widget=forms.TextInput(attrs=INPUT))
    servings = forms.IntegerField(required=False, min_value=1, max_value=100,
                                  widget=forms.NumberInput(attrs={**INPUT, "inputmode": "numeric"}))
    prep_minutes = forms.IntegerField(label="Prep (min)", **MINUTES)
    cook_minutes = forms.IntegerField(label="Cook (min)", **MINUTES)
    total_time_minutes = forms.IntegerField(label="Total (min)", **MINUTES)
    description = forms.CharField(required=False, max_length=5000, widget=forms.Textarea(attrs={**INPUT, "rows": 2}))
    ingredients_text = forms.CharField(label="Ingredients (one per line)", required=False,
                                       widget=forms.Textarea(attrs={**INPUT, "rows": 10}))
    steps_text = forms.CharField(label='Steps (one per line; "## Sauce" starts a section)', required=False,
                                 widget=forms.Textarea(attrs={**INPUT, "rows": 10}))

    @classmethod
    def for_import(cls, data, import_id):
        return cls(initial={
            "import_id": import_id, "title": data.get("title"), "servings": data.get("servings"),
            "prep_minutes": data.get("prep_minutes"), "cook_minutes": data.get("cook_minutes"),
            "total_time_minutes": data.get("total_time_minutes"), "description": data.get("description"),
            "ingredients_text": "\n".join(importer.ingredient_lines(data.get("ingredients"))),
            "steps_text": "\n".join(data.get("steps") or [])})

    def clean(self):
        cleaned = super().clean()
        for key, limit in (("ingredients_text", importer.MAX_INGREDIENTS), ("steps_text", importer.MAX_STEPS)):
            if len([line for line in (cleaned.get(key) or "").splitlines() if line.strip()]) > limit:
                self.add_error(key, f"At most {limit} lines.")
        return cleaned

    def recipe_fields(self, data):
        """The edited fields, as import data. A line left as read keeps its original text and parts."""
        read = {i.get("original"): i for i in data.get("ingredients") or [] if isinstance(i, dict) and i.get("original")}
        cleaned = self.cleaned_data
        return {
            "title": cleaned["title"].strip(), "description": cleaned["description"].strip(),
            "servings": cleaned["servings"], "prep_minutes": cleaned["prep_minutes"],
            "cook_minutes": cleaned["cook_minutes"], "total_time_minutes": cleaned["total_time_minutes"],
            "ingredients": [read.get(line.strip()) or importer.parse_ingredient(line)
                            for line in cleaned["ingredients_text"].splitlines() if line.strip()],
            "steps": [line.strip()[:2000] for line in cleaned["steps_text"].splitlines() if line.strip()],
        }


class MealsSettingsForm(forms.ModelForm):
    class Meta:
        model = MealsSettings
        fields = ["digest_tonight"]
