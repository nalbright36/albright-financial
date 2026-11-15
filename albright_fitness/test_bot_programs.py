"""Bot API: programs - list, create (the guided setup in one call), pause / resume; exercise names never guessed."""
import json
from datetime import time, timedelta

from django.test import Client

from albright_bot_api.models import BotToken
from albright_planner.models import ScheduleItem

from . import programs
from .models import Exercise, Program
from .test_body import API, Base

UPPER_LOWER = {"name": "Bot split", "days": [
    {"name": "Upper", "weekdays": ["mon", "thu"], "exercises": [{"exercise": "barbell bench press", "sets": 4, "reps": 6},
                                                                {"exercise": "Pull-Up"}]},
    {"name": "Cardio", "weekdays": [5], "exercises": [{"exercise": "Running", "minutes": 30}]}]}


class BotProgramTests(Base):
    def call(self, method, path, raw, body=None):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {raw}"}
        if method == "GET":
            return Client().get(API + path, **headers)
        return Client().post(API + path, data=json.dumps(body or {}), content_type="application/json", **headers)

    def test_create_on_the_planner_then_list_pause_resume(self):
        raw = BotToken.issue(self.me, "Coach", ["fitness:read", "fitness:write"])[1]
        r = self.call("POST", "fitness/programs", raw, {**UPPER_LOWER, "planner": True, "time": "06:30", "weeks": 8,
                                                         "start_date": (self.today + timedelta(days=1)).isoformat()})
        self.assertEqual(r.status_code, 201, r.content)
        data = r.json()
        self.assertEqual((data["status"], data["weeks"], data["planner"], data["time"], data["planner_tasks"]),
                         ("active", 8, True, "06:30", 2))
        self.assertEqual(data["days"][0]["weekdays"], [0, 3])
        self.assertEqual([e["target"] for e in data["days"][0]["exercises"]], ["4 × 6", "3 × 10"])
        self.assertEqual(data["days"][1]["exercises"][0]["target"], "30 min")
        self.assertEqual(ScheduleItem.objects.filter(owner=self.me, source_app="fitness").first().start_time, time(6, 30))
        listed = self.call("GET", "fitness/programs", raw).json()["programs"]
        self.assertEqual([p["name"] for p in listed], ["Bot split"])
        pause = self.call("POST", f"fitness/programs/{data['id']}/pause", raw)
        self.assertEqual((pause.status_code, pause.json()["status"], pause.json()["planner"]), (200, "paused", True))
        resume = self.call("POST", f"fitness/programs/{data['id']}/resume", raw).json()
        self.assertEqual((resume["status"], resume["on_planner_now"]), ("active", True))

    def test_unknown_exercises_create_nothing(self):
        raw = BotToken.issue(self.me, "Coach", ["fitness:write"])[1]
        body = {"name": "Oops", "days": [{"name": "A", "weekdays": [0], "exercises": [{"exercise": "Bench"}, {"exercise": "Zorbing"}]}]}
        r = self.call("POST", "fitness/programs", raw, body)
        self.assertEqual(r.status_code, 422)
        self.assertEqual([u["exercise"] for u in r.json()["unknown"]], ["Bench", "Zorbing"])
        self.assertIn("Barbell Bench Press", r.json()["unknown"][0]["suggestions"] + ["Barbell Bench Press"])
        self.assertFalse(Program.objects.exists())
        self.assertEqual(self.call("POST", "fitness/programs", raw, {"name": "X", "days": [{"name": "A", "weekdays": [9]}]}).status_code, 400)
        self.assertEqual(self.call("POST", "fitness/programs", raw, {**UPPER_LOWER, "time": "6.30am"}).status_code, 400)

    def test_scopes_and_privacy(self):
        read = BotToken.issue(self.me, "Reader", ["fitness:read"])[1]
        self.assertEqual(self.call("POST", "fitness/programs", read, UPPER_LOWER).status_code, 403)
        write = BotToken.issue(self.me, "Writer", ["fitness:write"])[1]
        self.assertEqual(self.call("GET", "fitness/programs", write).status_code, 403)
        theirs = programs.copy_template(self.wife, "running")
        self.assertEqual(self.call("POST", f"fitness/programs/{theirs.pk}/pause", write).status_code, 404)
        self.assertNotIn("Running 3x/week", self.call("GET", "fitness/programs", read).content.decode())
        draft = programs.copy_template(self.me, "running", active=False)
        self.assertEqual(self.call("POST", f"fitness/programs/{draft.pk}/resume", write).status_code, 409)
        Exercise.objects.create(owner=self.wife, name="Sam's lift")
        body = {"name": "X", "days": [{"name": "A", "weekdays": [0], "exercises": [{"exercise": "Sam's lift"}]}]}
        self.assertEqual(self.call("POST", "fitness/programs", write, body).status_code, 422)   # not in my library
