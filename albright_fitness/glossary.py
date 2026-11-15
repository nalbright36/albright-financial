"""Plain words for Fitness's gym and nutrition terms - the small "?" next to a term ({% term "rpe" %},
fitness_extras) and the workout screen's "How to log sets". One place, so every page explains a term the same way."""

TERMS = {
    "rpe": ("RPE", "How hard the set felt, from 1 to 10. 10 means you couldn't do another rep; 8 means about two "
                   "left in the tank. Optional."),
    "e1rm": ("Estimated 1-rep max", "The most you could probably lift once, worked out from a set you did (for example "
                                    "185 lb × 8). It lets sets with different reps be compared."),
    "volume": ("Volume", "Weight × reps, added up over every done set. A rough measure of how much work you did."),
    "working_sets": ("Working sets", "The sets that count - your real sets, not warm-ups. Each done set is counted "
                                     "for the muscles the exercise mainly works."),
    "set_types": ("Set types", "Tap a set's number to switch it: a number is a normal (working) set, W is a warm-up "
                               "(not counted toward records or volume), D is a drop set (lighter weight right after a set)."),
    "targets": ("Targets", "What to aim for each time: sets and reps for lifts, or minutes and/or miles for cardio "
                           "and timed holds. Leave any of them empty."),
    "done": ("The done box", "Tick it when you finish a set. That saves it as done and starts the rest timer at the "
                             "bottom of the screen."),
}


def get(key):
    return TERMS[key]
