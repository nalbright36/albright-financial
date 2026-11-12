"""The full "New item" / edit form. Repeats are picked from plain choices
(recurrence.REPEATS) - nobody types an RRULE; clean() turns them into one."""
from django import forms

from . import recurrence
from .models import NOTES_MAX, ScheduleItem
from .planner import SCOPES

COLORS = [("", "App color"), ("#EF4444", "Red"), ("#F97316", "Orange"), ("#EAB308", "Yellow"), ("#22C55E", "Green"),
          ("#3B82F6", "Blue"), ("#A855F7", "Purple"), ("#EC4899", "Pink"), ("#64748B", "Gray")]
ENDS = [("never", "Never"), ("on", "On a date"), ("after", "After a number of times")]


class ItemForm(forms.Form):
    title = forms.CharField(max_length=200)
    kind = forms.ChoiceField(choices=ScheduleItem.KINDS, initial=ScheduleItem.TASK, widget=forms.RadioSelect)
    date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    start_time = forms.TimeField(required=False, widget=forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
                                 help_text="Leave empty for all day.")
    end_time = forms.TimeField(required=False, widget=forms.TimeInput(attrs={"type": "time"}, format="%H:%M"))
    checkable = forms.BooleanField(required=False, initial=True, label="Can be checked off",
                                   help_text="Tasks always can; turn it on for an event you want to tick.")
    priority = forms.ChoiceField(choices=ScheduleItem.PRIORITIES, initial="none")
    color = forms.ChoiceField(choices=COLORS, required=False)
    # No max_length here: it would put maxlength on the textarea, and the browser would cut a long paste off
    # without a word. clean_notes() checks the length and says so on the form, which keeps the text.
    notes = forms.CharField(required=False, strip=False,
                            widget=forms.Textarea(attrs={"rows": 14, "data-md-input": "", "spellcheck": "true"}))

    repeat = forms.ChoiceField(choices=recurrence.REPEATS, initial="none", label="Repeat")
    days = forms.MultipleChoiceField(choices=list(zip(recurrence.WEEKDAYS, recurrence.DAY_NAMES)), required=False,
                                     widget=forms.CheckboxSelectMultiple, label="On")
    interval = forms.IntegerField(min_value=1, max_value=52, initial=2, required=False, label="Every N weeks")
    day_interval = forms.IntegerField(min_value=2, max_value=365, initial=3, required=False, label="Every N days")
    month_day = forms.IntegerField(min_value=1, max_value=31, required=False, label="Day of the month")
    nth = forms.TypedChoiceField(choices=recurrence.NTH_CHOICES, coerce=int, required=False, label="Which")
    nth_weekday = forms.ChoiceField(choices=list(zip(recurrence.WEEKDAYS, recurrence.LONG_DAY_NAMES)), required=False,
                                    label="Weekday")
    ends = forms.ChoiceField(choices=ENDS, initial="never", widget=forms.RadioSelect, label="Ends")
    until = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}), label="End date")
    count = forms.IntegerField(min_value=1, max_value=1000, required=False, label="Number of times")
    repeat_from_completion = forms.BooleanField(
        required=False, label="Repeat from completion date",
        help_text="The next one is due that long after you check this one off, instead of on the next scheduled day. "
                  "Tasks that never end show one at a time either way.")
    scope = forms.ChoiceField(choices=SCOPES, initial="this", widget=forms.RadioSelect, required=False,
                              label="Apply this change to")
    location = forms.CharField(
        required=False, max_length=200, label="Location",
        help_text="Leave empty to use your home location (Planner settings). For a trip, or anything that isn't at home.")

    def __init__(self, *args, show_scope=False, saved_place=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.show_scope = show_scope
        # saved_place: (label, lat, lon) already on the item, so an unchanged
        # field doesn't geocode again. self.place is set in clean_location.
        self.saved_place = saved_place
        self.place = saved_place or ("", None, None)
        for name, field in self.fields.items():
            if not isinstance(field.widget, (forms.RadioSelect, forms.CheckboxSelectMultiple, forms.CheckboxInput)):
                field.widget.attrs.setdefault("class", "pl-input")
        if not show_scope:
            del self.fields["scope"]

    def clean_notes(self):
        notes = self.cleaned_data.get("notes") or ""
        if len(notes) > NOTES_MAX:
            raise forms.ValidationError(
                f"Notes can be up to {NOTES_MAX:,} characters - these are {len(notes):,}. "
                f"Shorten them by {len(notes) - NOTES_MAX:,} and save again; your text is still here.")
        return notes

    @classmethod
    def initial_for(cls, item, occurrence_date, viewer=None):
        """The form for one occurrence; viewer (a ZoneInfo): its date and times shown in that zone."""
        from .planner import convert, zone
        rule = recurrence.describe(item.recurrence)
        day, start, end = occurrence_date, item.start_time, item.end_time
        if viewer is not None:
            day, start, end = convert(day, start, end, zone(item.timezone), viewer)
        return {
            "title": item.title, "kind": item.kind, "date": day, "start_time": start,
            "end_time": end, "checkable": item.checkable, "priority": item.priority, "color": item.color,
            "notes": item.notes, "repeat": rule["repeat"], "days": rule["days"],
            "interval": rule["interval"] if rule["repeat"] == "every_n_weeks" else 2,
            "day_interval": rule["interval"] if rule["repeat"] == "every_n_days" else 3,
            "repeat_from_completion": item.repeat_from_completion,
            "month_day": rule["month_day"], "nth": rule["nth"], "nth_weekday": rule["nth_weekday"],
            "ends": "on" if item.recurrence_until else "after" if item.recurrence_count else "never",
            "until": item.recurrence_until, "count": item.recurrence_count, "scope": "this",
            "location": item.location_label,
        }

    def clean_location(self):
        raw = (self.cleaned_data.get("location") or "").strip()
        if not raw:
            self.place = ("", None, None)
            return ""
        saved = self.saved_place
        if saved and saved[1] is not None and raw == saved[0]:
            self.place = saved
            return saved[0]
        from . import weather
        try:
            found = weather.geocode(raw)
        except weather.LookupDown:
            raise forms.ValidationError("Couldn't look up that place right now - try again in a moment.")
        if not found:
            raise forms.ValidationError("Couldn't find that place - try a city and state, like Lone Tree, CO.")
        self.place = (found["label"], found["lat"], found["lon"])
        return found["label"]

    def clean(self):
        data = super().clean()
        start, end = data.get("start_time"), data.get("end_time")
        if end and not start:
            self.add_error("start_time", "Add a start time, or clear the end time for an all-day item.")
        elif start and end and end <= start:
            self.add_error("end_time", "The end time must be after the start.")
        if data.get("ends") == "on" and data.get("repeat") != "none" and not data.get("until"):
            self.add_error("until", "Pick the date it ends.")
        if data.get("ends") == "on" and data.get("until") and data.get("date") and data["until"] < data["date"]:
            self.add_error("until", "The end date is before the item's date.")
        if data.get("ends") == "after" and data.get("repeat") != "none" and not data.get("count"):
            self.add_error("count", "How many times?")
        if data.get("repeat") in ("weekly", "every_n_weeks") and not data.get("days") and data.get("date"):
            data["days"] = [recurrence.WEEKDAYS[data["date"].weekday()]]
        return data

    def changes(self):
        """The cleaned form as planner.change()'s changes."""
        d = self.cleaned_data
        interval = (d.get("day_interval") or 3) if d["repeat"] == "every_n_days" else d.get("interval") or 1
        rule = recurrence.build(d["repeat"], d["date"], days=d.get("days") or (), interval=interval,
                                month_day=d.get("month_day"), nth=d.get("nth"), nth_weekday=d.get("nth_weekday"))
        kind = d["kind"]
        return {
            "title": d["title"].strip(), "kind": kind, "date": d["date"], "start_time": d.get("start_time"),
            "end_time": d.get("end_time") if d.get("start_time") else None,
            "checkable": True if kind == ScheduleItem.TASK else bool(d.get("checkable")),
            "priority": d.get("priority") or "none", "color": d.get("color") or "", "notes": d.get("notes", "").strip(),
            "recurrence": rule,
            "recurrence_until": d.get("until") if rule and d.get("ends") == "on" else None,
            "recurrence_count": d.get("count") if rule and d.get("ends") == "after" else None,
            "repeat_from_completion": bool(rule and kind == ScheduleItem.TASK and d.get("ends") == "never"
                                           and d.get("repeat_from_completion")),
            "location_label": (self.place[0] if self.place else "") or "",
            "location_lat": self.place[1] if self.place else None,
            "location_lon": self.place[2] if self.place else None,
        }


class GroceryTaskForm(forms.Form):
    """Meals' grocery list "Create task": the picked lines become the task's checklist."""
    title = forms.CharField(max_length=200, required=False)
    date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    time = forms.TimeField(required=False, widget=forms.TimeInput(attrs={"type": "time"}, format="%H:%M"))
    note = forms.CharField(max_length=1000, required=False)

    def clean_title(self):
        from .checklist import GROCERY_TITLE
        return (self.cleaned_data.get("title") or "").strip() or GROCERY_TITLE


class FromNoteForm(forms.Form):
    """Notes' "Add to schedule": a date, an optional time, and a simple repeat."""
    REPEATS = [(k, label) for k, label in recurrence.REPEATS if k in ("none", "daily", "weekdays", "weekly",
                                                                      "monthly_day", "yearly")]
    date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    time = forms.TimeField(required=False, widget=forms.TimeInput(attrs={"type": "time"}, format="%H:%M"))
    repeat = forms.ChoiceField(choices=REPEATS, initial="none", required=False)

    def clean_repeat(self):
        return self.cleaned_data.get("repeat") or "none"
