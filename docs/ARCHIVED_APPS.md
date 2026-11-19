# Archived apps

An archived app is switched off but kept whole, so it can be brought back later. Today **Trading** and **Reselling**
are archived.

## What archiving does (and doesn't)

Archiving changes **nothing in the database**: no tables are dropped, no rows deleted, no migrations removed. The app
stays in `INSTALLED_APPS` and all of its code stays. While an app is archived:

- **Hidden everywhere.** It's missing from the launcher, Home, the nav, "Open after sign-in", Admin > Users, invite
  forms, the landing page, notifications and links from other apps (Notes links show as plain "Trading record" text; the
  Planner's Reselling sources disappear).
- **Its pages** answer a plain "This app is archived" page with HTTP 404, signed in or not. **Superusers** can still open
  them, with an "Archived app - hidden from everyone else" banner, to look at old data. A page that needs the app's saved
  credentials (for example Alpaca) shows a note instead, because those credentials aren't used.
- **Background work stops.** Every management command of the app exits at once with `app archived, skipping`, so it
  makes no API calls, sends no Telegram alerts and never decrypts a stored secret.
- **Bot API.** Its scopes aren't offered on the API key pages and are left out of the docs, the OpenAPI spec and the MCP
  tools. Its endpoints answer 404. Existing keys keep the scopes they hold, so they work again on restore.
- **People keep their access entries.** Each person's stored allowed-app entry for it is kept (Admin > Users just stops
  showing it), so restoring brings access back. Anyone whose default app was an archived one lands on Home.
- **Stored secrets** (Alpaca keys) stay encrypted in the database but are never decrypted while archived.
  `rotate_encryption_key` still re-encrypts them; it's site-wide maintenance, not the app's work.

Everything goes through one helper: `albright_labs.archive.is_app_archived(slug)`.

## The setting

```python
# albright_trading_proj/settings.py
ARCHIVED_APPS = [...]  # from the ARCHIVED_APPS env var; default "trading,reselling"
```

Slugs can be any of an app's names: `trading` / `trade`, `reselling` / `resell`, or the package name. In `.env`:

```
ARCHIVED_APPS=trading,reselling   # the default when unset
ARCHIVED_APPS=reselling           # restore Trading only
ARCHIVED_APPS=                    # restore everything
```

Tests run with every app live (the test runner clears the setting), so the archived apps' own tests keep running.
`albright_labs/test_archive.py` switches archiving on with `override_settings`.

## Restoring an app

1. Take its slug out of `ARCHIVED_APPS` (in the server's `.env`, or the default in `settings.py`).
2. Deploy: pull, then reload the web app on PythonAnywhere. No migration is needed.
3. Turn its scheduled tasks back on (the checklist below).
4. Check Admin > System health and Admin > Integrations: its jobs and integrations reappear there.

## Scheduled tasks

PythonAnywhere > Tasks. While an app is archived these tasks only print `app archived, skipping`. **Pause or delete them**
(keep a note of each command and time so you can restore them).

The **always-on** tasks must be disabled. PythonAnywhere restarts an always-on task whenever it exits, so a skipped one
would just restart over and over.

### Trading

- [ ] `python manage.py run_strategy_engine`: **always-on**, the paper-trading bot runner (places orders on Alpaca)
- [ ] `python manage.py snapshot_strategies`: daily, after the market closes (Grok Bot P&L history)
- [ ] `python manage.py run_news_sentiment`: daily (Alpaca news, OpenAI)
- [ ] `python manage.py run_reddit_sentiment`: daily (Reddit)

### Reselling

- [ ] `python manage.py run_scan_worker`: **always-on**, processes scan, deep-dive and analysis requests (Anthropic)
- [ ] `python manage.py scan_lots ...`: hourly, one task per source (e.g. `--source hibid --min-interval-hours 6`)
- [ ] `python manage.py watchlist_alerts --loop-minutes 55 --every 5`: hourly (Telegram bid alerts)
- [ ] `python manage.py track_closed`: daily
- [ ] `python manage.py fetch_spot_prices`: daily (goldapi.io)
- [ ] `python manage.py alert_digest`: daily (Telegram digest; it also carried Meals' "Tonight: <dinner>" line, which
      stops while Reselling is archived)
- [ ] `python manage.py calibrate`: weekly or nightly
- [ ] `python manage.py prune_lots`: weekly

One-off tools (`export_features`, `import_personal_ledger`, `test_alert`) also skip while archived.

### Keep running (not part of an archived app)

`prune_activity`, `send_activity_digest`, `check_eat_out`, `sync_workout_task_completion`,
`cleanup_orphan_attachments`, `recalc_storage_usage`.

## Environment variables only an archived app uses

Keep them in `.env` so restoring works. They're simply unused while the app is archived.

- Trading: `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` (`ALPACA_CREDENTIALS_ENCRYPTION_KEY` stays in use, since it also
  encrypts Gmail and AI-assistant keys), `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`
- Reselling: `ANTHROPIC_API_KEY`, `GOLDAPI_KEY`, `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET`

`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` stay in use (server-error alerts, Admin > Integrations test) and so does
`OPENAI_API_KEY` (Meals recipe import, Opportunities).
