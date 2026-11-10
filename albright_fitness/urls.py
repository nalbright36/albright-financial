from django.urls import path

from . import body_views, challenge_views, food_views, views

app_name = "albright_fitness"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("challenges/", challenge_views.challenge_list, name="challenges"),
    path("challenges/new/", challenge_views.challenge_new, name="challenge_new"),
    path("challenges/<int:pk>/", challenge_views.challenge, name="challenge"),
    path("challenges/<int:pk>/board/", challenge_views.challenge_board, name="challenge_board"),
    path("challenges/<int:pk>/log/", challenge_views.challenge_log, name="challenge_log"),
    path("entries/<int:pk>/comment/", challenge_views.challenge_comment, name="challenge_comment"),
    path("exercises/", views.exercises, name="exercises"),
    path("exercises/<int:pk>/", views.exercise, name="exercise"),
    path("start/", views.start, name="start"),
    path("quick/", views.quick, name="quick"),
    path("workouts/<int:pk>/", views.workout, name="workout"),
    path("workouts/<int:pk>/edit/", views.edit_workout, name="edit_workout"),
    path("workouts/<int:pk>/delete/", views.delete_workout, name="delete_workout"),
    path("workouts/<int:pk>/exercises/add/", views.add_exercise, name="add_exercise"),
    path("workouts/<int:pk>/exercises/<int:we_pk>/remove/", views.remove_exercise, name="remove_exercise"),
    path("workouts/<int:pk>/finish/", views.finish, name="finish"),
    path("workouts/<int:pk>/summary/", views.summary, name="summary"),
    path("exercise-sets/<int:we_pk>/add/", views.add_set, name="add_set"),
    path("sets/<int:pk>/", views.update_set, name="update_set"),
    path("sets/<int:pk>/delete/", views.delete_set, name="delete_set"),
    path("history/", views.history, name="history"),
    path("programs/", views.program_list, name="programs"),
    path("programs/<int:pk>/", views.program, name="program"),
    path("programs/<int:pk>/delete/", views.program_delete, name="program_delete"),
    # Food (food_views): the diary, adding food, My foods, targets - each person's own.
    path("food/", food_views.diary, name="food"),
    path("food/day/<str:day>/", food_views.diary, name="food_day"),
    path("food/add/", food_views.add, name="food_add"),
    path("food/usda/<int:fdc_id>/", food_views.usda_food, name="food_usda"),
    path("food/log/", food_views.log, name="food_log"),
    path("food/copy/", food_views.copy_yesterday, name="food_copy"),
    path("food/entries/<int:pk>/delete/", food_views.delete_entry, name="food_delete"),
    path("food/mine/", food_views.my_foods, name="my_foods"),
    path("food/mine/<int:pk>/delete/", food_views.delete_my_food, name="my_food_delete"),
    path("targets/", food_views.targets, name="targets"),
    # Body metrics and Insights (body_views) - each person's own.
    path("body/", body_views.body_page, name="body"),
    path("body/weight/", body_views.log_weight, name="log_weight"),
    path("body/weight/<int:pk>/delete/", body_views.delete_weight, name="delete_weight"),
    path("body/measurements/", body_views.log_measurements, name="log_measurements"),
    path("body/settings/", body_views.body_settings, name="body_settings"),
    path("insights/", body_views.insights_page, name="insights"),
]
