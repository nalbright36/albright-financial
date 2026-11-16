""""Connect your AI" for the food log: the instructions a person pastes into their AI (custom instructions / a
project), and the OpenAPI spec (GET /api/bot/v1/fitness/openapi.json) for AIs that import custom actions or tools.

Both are built from the site's own address. Neither ever holds a key: the person puts their key in the AI's
secret / connection settings, never in the instructions or a chat.
"""
from django.conf import settings
from django.urls import reverse


def api_base(request):
    """https://<site>/api/bot/v1 - what every endpoint below hangs off (https in production: the API needs it, and
    behind the host's proxy the request itself can look like http)."""
    url = request.build_absolute_uri(reverse("bot_api_v1:docs")).rsplit("/", 1)[0]
    if not settings.DEBUG and url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    return url


def instructions(base):
    return f"""You can log what I eat to my food diary through my Albright Fitness API.

Connection
- Base URL: {base}
- Every request sends my key as the header "Authorization: Bearer <my key>". The key is in your connection /
  secret settings - never ask me to paste it in the chat, and never show it.
- Machine-readable spec: {base}/fitness/openapi.json

Endpoints
- GET  {base}/fitness/foods/search?q=<food>        real per-serving numbers (USDA foods, my foods, my recipes)
- GET  {base}/fitness/food-entries/recent?limit=20  my most-logged foods with their numbers ("the usual")
- POST {base}/fitness/food-entries                  log items: {{"date"?, "meal"?, "items": [...], "source_text",
                                                    "idempotency_key"}}
- GET  {base}/fitness/food-entries?date=YYYY-MM-DD  a day's entries, totals and my targets
- PATCH  {base}/fitness/food-entries/<id>           fix an entry (any field)
- DELETE {base}/fitness/food-entries/<id>           remove an entry

Each item: {{"name", "quantity", "unit", "grams" (optional), "calories", "protein_g", "carbs_g", "fat_g" (required),
"fiber_g", "sugar_g", "sodium_mg" (optional), "confidence": "high"|"medium"|"low", "notes"}}. Leave out "meal" and
"date" to use my local time (breakfast before 10:30, lunch before 15:00, dinner before 21:00, else snack).

How to log my food
- When I tell you what I ate, estimate the macros for each item. Check foods/search first for packaged or common
  foods and use those numbers when they match; otherwise estimate from typical portions.
- Ask me one short question only if the portion or a big-calorie item is genuinely unclear (e.g. "regular or
  large?"); otherwise just log it.
- Send everything I ate in one POST with an idempotency_key (a new random string per meal, reused only if you
  retry the same request), my exact words in source_text, and a confidence per item.
- If the answer has "warnings" (calories that don't match the macros), check that item and PATCH it if needed.
- After logging, reply with a short summary: each item with calories and protein, and my new day total vs. my
  target (from "totals" and "targets" in the response, e.g. "that puts you at 1,450 of 2,200 calories").
- If I say something was wrong, PATCH or DELETE that entry (by its id) instead of adding a new one.
- If you get a 400, read "errors" (each has a field and a message), fix those fields and send it again.
"""


_NUM = {"type": "number", "minimum": 0}


def _item_schema(required=True):
    props = {
        "name": {"type": "string", "maxLength": 200, "example": "Chipotle chicken burrito bowl"},
        "quantity": {"type": "number", "exclusiveMinimum": 0, "maximum": 5000, "example": 1},
        "unit": {"type": "string", "maxLength": 40, "description": "Free text: bowl, slice, cup, g ...", "example": "bowl"},
        "grams": {**_NUM, "maximum": 5000},
        "calories": {**_NUM, "maximum": 5000, "example": 720},
        "protein_g": {**_NUM, "maximum": 1000, "example": 48},
        "carbs_g": {**_NUM, "maximum": 1000, "example": 70},
        "fat_g": {**_NUM, "maximum": 1000, "example": 24},
        "fiber_g": {**_NUM, "maximum": 300},
        "sugar_g": {**_NUM, "maximum": 1000},
        "sodium_mg": {**_NUM, "maximum": 50000},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "notes": {"type": "string", "maxLength": 1000, "example": "white rice, black beans, mild salsa, cheese"},
    }
    schema = {"type": "object", "properties": props}
    if required:
        schema["required"] = ["name", "calories", "protein_g", "carbs_g", "fat_g"]
    return schema


def openapi_spec(base):
    meal = {"type": "string", "enum": ["breakfast", "lunch", "dinner", "snack"]}
    day = {"type": "object", "properties": {
        "date": {"type": "string", "format": "date"},
        "entries": {"type": "array", "items": {"$ref": "#/components/schemas/Entry"}},
        "totals": {"$ref": "#/components/schemas/Totals"},
        "targets": {"oneOf": [{"$ref": "#/components/schemas/Totals"}, {"type": "null"}]},
        "warnings": {"type": "array", "items": {"type": "string"}},
    }}
    error = {"description": "Field errors - fix and resend",
             "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}}
    entry_id = {"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}
    return {
        "openapi": "3.1.0",
        "info": {"title": "Albright Fitness food log", "version": "1.0.0",
                 "description": "Log, read and fix the key owner's food diary. Send the key as a Bearer token."},
        "servers": [{"url": base}],
        "security": [{"bearerAuth": []}],
        "paths": {
            "/fitness/food-entries": {
                "get": {"operationId": "getFoodDay", "summary": "A day's entries, totals and targets (fitness:food:read)",
                        "parameters": [{"name": "date", "in": "query", "schema": {"type": "string", "format": "date"},
                                        "description": "Default: the owner's today"}],
                        "responses": {"200": {"description": "The day", "content": {"application/json": {"schema": day}}}}},
                "post": {"operationId": "logFood",
                         "summary": "Log 1-20 items in one go - all saved or none (fitness:food:write)",
                         "requestBody": {"required": True, "content": {"application/json": {"schema": {
                             "type": "object", "required": ["items"], "properties": {
                                 "date": {"type": "string", "format": "date", "description": "Default: the owner's today"},
                                 "meal": {**meal, "description": "Default: by the owner's local time"},
                                 "items": {"type": "array", "minItems": 1, "maxItems": 20, "items": _item_schema()},
                                 "source_text": {"type": "string", "maxLength": 4000,
                                                 "description": "What the person said, verbatim"},
                                 "idempotency_key": {"type": "string", "maxLength": 100,
                                                     "description": "The same key within 24 hours returns the first result"},
                             }}}}},
                         "responses": {"201": {"description": "Logged", "content": {"application/json": {"schema": day}}},
                                       "200": {"description": "Replayed (same idempotency_key)",
                                               "content": {"application/json": {"schema": day}}},
                                       "400": error}},
            },
            "/fitness/food-entries/{id}": {
                "patch": {"operationId": "fixFoodEntry", "summary": "Fix any field of one of the owner's entries",
                          "parameters": [entry_id],
                          "requestBody": {"required": True, "content": {"application/json": {"schema": {
                              **_item_schema(required=False),
                              "properties": {**_item_schema(required=False)["properties"], "meal": meal,
                                             "date": {"type": "string", "format": "date"}}}}}},
                          "responses": {"200": {"description": "Changed"}, "400": error, "404": {"description": "Not theirs"}}},
                "delete": {"operationId": "deleteFoodEntry", "summary": "Remove one of the owner's entries",
                           "parameters": [entry_id],
                           "responses": {"200": {"description": "Removed"}, "404": {"description": "Not theirs"}}},
            },
            "/fitness/food-entries/recent": {
                "get": {"operationId": "recentFoods", "summary": "The owner's most-logged foods, ready to send back as items",
                        "parameters": [{"name": "limit", "in": "query", "schema": {"type": "integer", "minimum": 1,
                                                                                    "maximum": 50, "default": 20}}],
                        "responses": {"200": {"description": "Foods"}}},
            },
            "/fitness/foods/search": {
                "get": {"operationId": "searchFoods",
                        "summary": "USDA foods, the owner's foods and recipes, with per-serving numbers",
                        "parameters": [{"name": "q", "in": "query", "required": True, "schema": {"type": "string",
                                                                                                  "maxLength": 100}}],
                        "responses": {"200": {"description": "Matches by group"}}},
            },
        },
        "components": {
            "securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer"}},
            "schemas": {
                "Entry": {"type": "object", "properties": {
                    "id": {"type": "integer"}, "date": {"type": "string", "format": "date"}, "meal": meal,
                    **_item_schema(required=False)["properties"],
                    "source": {"type": "string"}, "edited": {"type": "boolean"}}},
                "Totals": {"type": "object", "properties": {
                    "calories": {"type": ["number", "null"]}, "protein_g": {"type": ["number", "null"]},
                    "carbs_g": {"type": ["number", "null"]}, "fat_g": {"type": ["number", "null"]}}},
                "Error": {"type": "object", "properties": {
                    "error": {"type": "string"},
                    "errors": {"type": "array", "items": {"type": "object", "properties": {
                        "field": {"type": "string", "example": "items[0].calories"}, "message": {"type": "string"}}}}}},
            },
        },
    }
