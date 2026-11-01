"""The full "New item" / edit form. Repeats are picked from plain choices
(recurrence.REPEATS) - nobody types an RRULE; clean() turns them into one."""
from django import forms

from . import recurrence
from .models import ScheduleItem
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
    notes = forms.CharField(max_length=1000, required=False, widget=forms.Textarea(attrs={"rows": 3}))

    repeat = forms.ChoiceField(choices=recurrence.REPEATS, initial="none", label="Repeat")
    days = forms.MultipleChoiceField(choices=list(zip(recurrence.WEEKDAYS, recurrence.DAY_NAMES)), required=False,
                                     widget=forms.CheckboxSelectMultiple, label="On")
    interval = forms.IntegerField(min_value=1, max_value=52, initial=2, required=False, label="Every N weeks")
    month_day = forms.IntegerField(min_value=1, max_value=31, required=False, label="Day of the month")
    nth = forms.TypedChoiceField(choices=recurrence.NTH_CHOICES, coerce=int, required=False, label="Which")
    nth_weekday = forms.ChoiceField(choices=list(zip(recurrence.WEEKDAYS, recurrence.LONG_DAY_NAMES)), required=False,
                                    label="Weekday")
    ends = forms.ChoiceField(choices=ENDS, initial="never", widget=forms.RadioSelect, label="Ends")
    until = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}), label="End date")
    count = forms.IntegerField(min_value=1, max_value=1000, required=False, label="Number of times")
    scope = forms.ChoiceField(choices=SCOPES, initial="this", widget=forms.RadioSelect, required=False,
                              label="Apply this change to")

    def __init__(self, *args, show_scope=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.show_scope = show_scope
        for name, field in self.fields.items():
            if not isinstance(field.widget, (forms.RadioSelect, forms.CheckboxSelectMultiple, forms.CheckboxInput)):
                field.widget.attrs.setdefault("class", "pl-input")
        if not show_scope:
            del self.fields["scope"]

    @classmethod
    def initial_for(cls, item, occurrence_date):
        rule = recurrence.describe(item.recurrence)
        return {
            "title": item.title, "kind": item.kind, "date": occurrence_date, "start_time": item.start_time,
            "end_time": item.end_time, "checkable": item.checkable, "priority": item.priority, "color": item.color,
            "notes": item.notes, "repeat": rule["repeat"], "days": rule["days"],
            "interval": rule["interval"] if rule["repeat"] == "every_n_weeks" else 2,
            "month_day": rule["month_day"], "nth": rule["nth"], "nth_weekday": rule["nth_weekday"],
            "ends": "on" if item.recurrence_until else "after" if item.recurrence_count else "never",
            "until": item.recurrence_until, "count": item.recurrence_count, "scope": "this",
        }

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
        rule = recurrence.build(d["repeat"], d["date"], days=d.get("days") or (), interval=d.get("interval") or 1,
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
        }
