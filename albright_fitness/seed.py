"""The built-in exercise library (migration 0002 adds these; seed() can run again safely - it only adds missing
ones). (name, kind, primary muscles, secondary muscles, equipment)."""
S, C, B, M = "strength", "cardio", "bodyweight", "mobility"

EXERCISES = [
    # ---- Barbell ----
    ("Barbell Back Squat", S, ["quads", "glutes"], ["hamstrings", "core"], "barbell"),
    ("Barbell Front Squat", S, ["quads"], ["glutes", "core"], "barbell"),
    ("Barbell Bench Press", S, ["chest"], ["triceps", "shoulders"], "barbell"),
    ("Incline Barbell Bench Press", S, ["chest"], ["shoulders", "triceps"], "barbell"),
    ("Deadlift", S, ["hamstrings", "glutes", "back"], ["forearms", "core"], "barbell"),
    ("Romanian Deadlift", S, ["hamstrings"], ["glutes", "back"], "barbell"),
    ("Sumo Deadlift", S, ["glutes", "hamstrings"], ["quads", "back"], "barbell"),
    ("Overhead Press", S, ["shoulders"], ["triceps", "core"], "barbell"),
    ("Barbell Row", S, ["back"], ["biceps", "forearms"], "barbell"),
    ("Pendlay Row", S, ["back"], ["biceps"], "barbell"),
    ("Barbell Curl", S, ["biceps"], ["forearms"], "barbell"),
    ("Close-Grip Bench Press", S, ["triceps"], ["chest", "shoulders"], "barbell"),
    ("Barbell Hip Thrust", S, ["glutes"], ["hamstrings"], "barbell"),
    ("Barbell Lunge", S, ["quads", "glutes"], ["hamstrings"], "barbell"),
    ("Barbell Shrug", S, ["back"], ["forearms"], "barbell"),
    ("Good Morning", S, ["hamstrings"], ["back", "glutes"], "barbell"),
    ("Power Clean", S, ["full_body"], ["back", "quads"], "barbell"),
    # ---- Dumbbell ----
    ("Dumbbell Bench Press", S, ["chest"], ["triceps", "shoulders"], "dumbbell"),
    ("Incline Dumbbell Press", S, ["chest"], ["shoulders", "triceps"], "dumbbell"),
    ("Dumbbell Fly", S, ["chest"], ["shoulders"], "dumbbell"),
    ("Dumbbell Shoulder Press", S, ["shoulders"], ["triceps"], "dumbbell"),
    ("Lateral Raise", S, ["shoulders"], [], "dumbbell"),
    ("Rear Delt Fly", S, ["shoulders"], ["back"], "dumbbell"),
    ("One-Arm Dumbbell Row", S, ["back"], ["biceps"], "dumbbell"),
    ("Dumbbell Curl", S, ["biceps"], ["forearms"], "dumbbell"),
    ("Hammer Curl", S, ["biceps", "forearms"], [], "dumbbell"),
    ("Overhead Triceps Extension", S, ["triceps"], [], "dumbbell"),
    ("Dumbbell Skull Crusher", S, ["triceps"], [], "dumbbell"),
    ("Goblet Squat", S, ["quads", "glutes"], ["core"], "dumbbell"),
    ("Dumbbell Lunge", S, ["quads", "glutes"], ["hamstrings"], "dumbbell"),
    ("Bulgarian Split Squat", S, ["quads", "glutes"], ["hamstrings"], "dumbbell"),
    ("Dumbbell Romanian Deadlift", S, ["hamstrings"], ["glutes"], "dumbbell"),
    ("Dumbbell Step-Up", S, ["quads", "glutes"], [], "dumbbell"),
    ("Dumbbell Calf Raise", S, ["calves"], [], "dumbbell"),
    ("Arnold Press", S, ["shoulders"], ["triceps"], "dumbbell"),
    ("Dumbbell Pullover", S, ["chest", "back"], [], "dumbbell"),
    # ---- Machines and cables ----
    ("Leg Press", S, ["quads", "glutes"], ["hamstrings"], "machine"),
    ("Leg Extension", S, ["quads"], [], "machine"),
    ("Lying Leg Curl", S, ["hamstrings"], [], "machine"),
    ("Seated Leg Curl", S, ["hamstrings"], [], "machine"),
    ("Hack Squat", S, ["quads"], ["glutes"], "machine"),
    ("Machine Chest Press", S, ["chest"], ["triceps"], "machine"),
    ("Pec Deck", S, ["chest"], [], "machine"),
    ("Machine Shoulder Press", S, ["shoulders"], ["triceps"], "machine"),
    ("Seated Calf Raise", S, ["calves"], [], "machine"),
    ("Standing Calf Raise", S, ["calves"], [], "machine"),
    ("Hip Abduction", S, ["glutes"], [], "machine"),
    ("Hip Adduction", S, ["quads"], [], "machine"),
    ("Lat Pulldown", S, ["back"], ["biceps"], "cable"),
    ("Seated Cable Row", S, ["back"], ["biceps"], "cable"),
    ("Cable Fly", S, ["chest"], ["shoulders"], "cable"),
    ("Triceps Pushdown", S, ["triceps"], [], "cable"),
    ("Cable Curl", S, ["biceps"], [], "cable"),
    ("Face Pull", S, ["shoulders", "back"], [], "cable"),
    ("Cable Crunch", S, ["core"], [], "cable"),
    ("Smith Machine Squat", S, ["quads", "glutes"], [], "machine"),
    # ---- Kettlebell ----
    ("Kettlebell Swing", S, ["glutes", "hamstrings"], ["core", "back"], "kettlebell"),
    ("Kettlebell Goblet Squat", S, ["quads", "glutes"], ["core"], "kettlebell"),
    ("Turkish Get-Up", S, ["full_body"], ["shoulders", "core"], "kettlebell"),
    # ---- Bodyweight ----
    ("Push-Up", B, ["chest"], ["triceps", "shoulders"], "bodyweight"),
    ("Pull-Up", B, ["back"], ["biceps"], "bodyweight"),
    ("Chin-Up", B, ["back", "biceps"], [], "bodyweight"),
    ("Dip", B, ["triceps", "chest"], ["shoulders"], "bodyweight"),
    ("Inverted Row", B, ["back"], ["biceps"], "bodyweight"),
    ("Bodyweight Squat", B, ["quads", "glutes"], [], "bodyweight"),
    ("Walking Lunge", B, ["quads", "glutes"], ["hamstrings"], "bodyweight"),
    ("Glute Bridge", B, ["glutes"], ["hamstrings"], "bodyweight"),
    ("Plank", B, ["core"], ["shoulders"], "bodyweight"),
    ("Side Plank", B, ["core"], [], "bodyweight"),
    ("Hanging Leg Raise", B, ["core"], ["forearms"], "bodyweight"),
    ("Crunch", B, ["core"], [], "bodyweight"),
    ("Mountain Climber", B, ["core"], ["shoulders", "cardio"], "bodyweight"),
    ("Burpee", B, ["full_body"], ["cardio"], "bodyweight"),
    ("Box Jump", B, ["quads", "glutes"], ["calves"], "other"),
    # ---- Cardio ----
    ("Running", C, ["cardio"], ["quads", "calves"], "none"),
    ("Treadmill Run", C, ["cardio"], ["quads", "calves"], "cardio_machine"),
    ("Walking", C, ["cardio"], [], "none"),
    ("Cycling", C, ["cardio"], ["quads"], "none"),
    ("Stationary Bike", C, ["cardio"], ["quads"], "cardio_machine"),
    ("Rowing Machine", C, ["cardio"], ["back", "quads"], "cardio_machine"),
    ("Elliptical", C, ["cardio"], [], "cardio_machine"),
    ("Stair Climber", C, ["cardio"], ["glutes", "calves"], "cardio_machine"),
    ("Swimming", C, ["cardio"], ["back", "shoulders"], "none"),
    ("Jump Rope", C, ["cardio"], ["calves"], "other"),
    # ---- Mobility ----
    ("Hip Flexor Stretch", M, ["quads"], [], "none"),
    ("Hamstring Stretch", M, ["hamstrings"], [], "none"),
    ("Foam Rolling", M, ["full_body"], [], "other"),
]


def seed(Exercise):
    """Add the built-in exercises that aren't there yet (by name). Exercise: the model (a migration passes its
    historical one)."""
    have = set(Exercise.objects.filter(owner__isnull=True).values_list("name", flat=True))
    Exercise.objects.bulk_create([
        Exercise(name=name, kind=kind, primary_muscles=primary, secondary_muscles=secondary, equipment=equipment)
        for name, kind, primary, secondary, equipment in EXERCISES if name not in have])
