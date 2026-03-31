# Customer Outreach Agent

A lightweight outreach automation tool for PMs and growth teams. It identifies users at behavioral milestones in your product, sends personalized interview or research emails via Gmail (or pushes contacts into ActiveCampaign), and throttles volume automatically based on your Calendly availability.

Runs daily via GitHub Actions. Zero infrastructure to manage beyond a Google Sheet and some GitHub Secrets.

---

## What it does

1. **Capacity check** — For `calendly` profiles: counts your open interview slots across the next 2 weeks. Exits early if none. For `fixed` profiles: skips this step.
2. **Booking sync** — For `calendly` profiles: fetches Calendly invitees (past 60 days) and auto-marks matching rows in your tracking sheet as `booked: yes`. No manual upkeep.
3. **Adaptive volume** — Calculates how many emails to send using the historical booking rate from your sheet (`ceil(open_slots / booking_rate)`). Falls back to `emails_per_slot x slots` until you have 30+ sends of history.
4. **Deduplication** — Loads all previously contacted emails from your Google Sheet. Nobody gets emailed twice.
5. **BigQuery query** — Pulls users matching your milestone criteria from BigQuery.
6. **Send** — Gmail: sends HTML + plain-text emails from your address directly. ActiveCampaign: upserts contacts and subscribes them to a list so AC handles deliverability and unsubscribes.
7. **Sheet update** — Appends newly contacted emails with date + batch label.
8. **Summary** — Sends you a summary email (and optional Slack alert on failure) with slot counts, conversion rates, send totals, and booking counts.

---

## How to set this up

### What you need

| Service | Purpose |
|---------|---------|
| **Google BigQuery** | Source of truth for user milestone data |
| **Google Sheets** | Deduplication tracking and conversion rate history |
| **Google Cloud service account** | Authenticates to both BigQuery and Sheets |
| **Gmail** + App Password | Sends outreach emails directly from your address |
| **Calendly** | (Optional) Throttles send volume to match interview availability |
| **ActiveCampaign** | (Optional) Alternative to Gmail for higher-volume sends |
| **Slack webhook** | (Optional) Failure alerts |

None of these are hard requirements — they're just what the agent is built on out of the box. Each integration is a small, self-contained section of `main.py`, so swapping one out is straightforward:

| If you're using... | Instead of... | Swap out... |
|--------------------|--------------|-------------|
| Snowflake, Redshift, Postgres, etc. | BigQuery | `get_bq_client()` + `run_bq_query()` (~20 lines) |
| Cal.com, Acuity, HubSpot meetings, etc. | Calendly | The Calendly helpers section (~80 lines) |
| Airtable, Notion, a database table, etc. | Google Sheets | The Google Sheets helpers (~60 lines) |
| SendGrid, Postmark, SES, etc. | Gmail SMTP | `send_emails()` (~20 lines) |

---

### Step 1: Fork this repo

Fork or clone this repo and set it up as a private or public GitHub repository.

---

### Step 2: Set up Google Cloud

1. Create a GCP project (or use an existing one). Find your **Project ID** in the top bar of the GCP Console — it looks like `my-company-prod`, not the display name.

2. Enable these two APIs (both required):
   - **BigQuery API**
   - **Google Sheets API**

   Go to **APIs & Services > Enable APIs and Services**, search for each, and click Enable.

3. Go to **IAM & Admin > Service Accounts** and create a new service account. The name doesn't matter.

4. Grant it these roles:
   - `BigQuery Data Viewer`
   - `BigQuery Job User`

5. After creating it, click into the service account > **Keys tab > Add Key > Create new key > JSON**. Download the file. You'll paste its full contents as a GitHub Secret — keep it somewhere safe.

   The service account's email address (shown on the service account page) looks like `something@your-project-id.iam.gserviceaccount.com`. You'll need this in the next step.

---

### Step 3: Set up your tracking sheet

1. Create a Google Sheet. Add this header row in row 1:

   | email | batch | booked |
   |-------|-------|--------|

2. Share the sheet with **Editor** access to the service account email from Step 2.

3. Copy the sheet ID from the URL — it's the long string between `/d/` and `/edit`:
   `https://docs.google.com/spreadsheets/d/`**`1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms`**`/edit`

---

### Step 4: Get your Calendly API key

*(Skip this step if you're using `capacity.type: fixed` instead of `calendly`.)*

1. Log in to Calendly and go to **Account > Integrations > API & Webhooks**.
2. Under **Personal Access Tokens**, click **Generate New Token**. Give it any name.
3. Copy the token — you'll add it as a GitHub Secret. You won't be able to see it again.

#### Find your event slug

The `event_slug` in `config.yml` is not your Calendly username — it's the path from your event's shareable link. To find it:

1. Go to **Event Types** in Calendly and click the event you want to use.
2. Click **Copy Link**. The link looks like:
   `https://calendly.com/d/`**`abc-xyz/your-event-name`**
3. The slug is everything after `/d/` — in this example: `abc-xyz/your-event-name`.

Paste that value into `config.yml` as `event_slug`.

---

### Step 5: Configure your profile in `config.yml`

Open `config.yml` and fill in the `interview_outreach` profile (or copy it to create a new one):

```yaml
interview_outreach:
  capacity:
    type: calendly
    event_slug: "abc-xyz/your-event-name"  # path after /d/ in your Calendly link
    weekly_meeting_cap: 4                   # max interviews per week

  outbound:
    type: gmail

  sheets_id: "your-google-sheet-id"

  sender:
    name: "Your Name"
    title: "Product Manager"
    email: "you@yourcompany.com"
    company_name: "Your Company"
    company_url: "https://www.yourcompany.com"
    logo_url: ""  # optional: URL to a hosted logo image (160px wide)

  batches:
    - label: "BATCH_A"
      emails_per_slot: 50
      max_emails_per_run: 500
      subject: "[Your subject line]"
      query: |
        SELECT u.email
        FROM `{project}.{dataset}.your_users_table` AS u
        -- ... your milestone query here
        LIMIT {limit}
      body_text: |
        [Your outreach email — use {calendly_link} for the booking link]
      body_html: |
        <p>[Your outreach email in HTML]</p>
```

#### Writing your BigQuery queries

Your queries need to return an `email` column. Use `{project}`, `{dataset}`, and `{limit}` as placeholders — they're substituted at runtime from your GitHub Secrets.

The `config.yml` includes two example query patterns:

- **Recently signed up, not yet at first milestone** — reaches users while they're still exploring
- **Hit milestone A, not yet at milestone B** — reaches users who engaged but dropped off

Adapt the table and column names to match your actual data model.

#### Capacity types

| Type | When to use |
|------|-------------|
| `calendly` | You're scheduling interviews — volume scales with open slots |
| `fixed` | Marketing campaigns — always send `max_emails_per_run` per run |

#### Outbound types

| Type | When to use |
|------|-------------|
| `gmail` | Low volume, personal researcher/PM voice |
| `activecampaign` | Higher volume, need unsubscribe management, AC sequences |

---

### Step 6: Add GitHub Secrets

Go to your repo > **Settings > Secrets and variables > Actions > New repository secret** and add:

| Secret | Required for | Notes |
|--------|-------------|-------|
| `CALENDLY_API_KEY` | `calendly` capacity | Personal access token from Step 4 |
| `GMAIL_ADDRESS` | all profiles | Your full Gmail address |
| `GMAIL_APP_PASSWORD` | gmail outbound | See below |
| `BIGQUERY_PROJECT` | all profiles | GCP project ID (not display name), e.g. `my-company-prod` |
| `BIGQUERY_DATASET` | all profiles | BigQuery dataset name, e.g. `analytics` |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | all profiles | Paste the **entire contents** of the JSON key file from Step 2 |
| `SLACK_WEBHOOK_URL` | optional | Slack incoming webhook for failure alerts |
| `ACTIVECAMPAIGN_API_URL` | AC outbound | Your AC account URL, e.g. `https://youraccount.api-us1.com` |
| `ACTIVECAMPAIGN_API_KEY` | AC outbound | ActiveCampaign > Settings > Developer |

**Getting a Gmail App Password:**
App Passwords only appear if your Google account has 2-Step Verification turned on.
1. Enable 2-Step Verification at myaccount.google.com/security if you haven't already.
2. Go to myaccount.google.com/apppasswords.
3. Create a new app password (name it anything, e.g. "Outreach Agent").
4. Copy the 16-character password — paste it as the `GMAIL_APP_PASSWORD` secret.

**Tip for `GOOGLE_SERVICE_ACCOUNT_JSON`:** Open the JSON file in a text editor, select all, and paste the entire thing as the secret value. It should start with `{` and end with `}`. Don't just paste the filename.

A `.env.example` file at the root of this repo lists all variables — copy it to `.env` for local development.

---

### Step 7: Test before you run for real

From the GitHub Actions tab, trigger **Outreach Automation** manually with:

- `dry_run: true` — runs the full flow (capacity check, BigQuery query, dedup) but sends nothing and writes nothing to the sheet. Great for verifying your query returns the right users.
- `test_email: your@email.com` — sends all batches to a single address instead of real users, skipping deduplication. Use this to preview exactly what recipients will see.

---

## Common errors

**`Permission denied` or `403` from BigQuery**
The service account is missing the `BigQuery Job User` role — it needs both Data Viewer and Job User.

**`APIError: [403]` from Google Sheets**
Either the Sheets API isn't enabled in your GCP project (Step 2), or the sheet hasn't been shared with the service account email (Step 3).

**`Event type not found for slug`**
The `event_slug` in `config.yml` doesn't match any event in your Calendly account. Double-check you're copying the path after `/d/` from the shareable link, not from the event type page URL.

**Gmail authentication failure**
You're using your regular Gmail password instead of an App Password. App Passwords are separate 16-character codes generated at myaccount.google.com/apppasswords and only appear after 2-Step Verification is enabled.

**`yaml.scanner.ScannerError` on startup**
Usually a YAML indentation problem in `config.yml`. Body text blocks that start with `|` need consistent indentation on every line.

---

## Running on a schedule

The workflow runs automatically on weekdays at 8:00 AM Pacific (`0 16 * * 1-5` UTC). Change the cron schedule in `.github/workflows/outreach.yml` to match your timezone or cadence.

---

## Multiple profiles

You can run multiple campaigns from the same repo by adding profiles to `config.yml`. Each profile has its own Calendly event (or fixed cap), BigQuery queries, tracking sheet, sender identity, and outbound channel.

To run a specific profile manually, use the `outreach_profile` input on the workflow dispatch. To run multiple profiles on different schedules, duplicate the workflow file.

---

## Tracking sheet format

Columns: `email | batch | booked`

- `batch` is written as `MMDDYY LABEL`, e.g. `032526 NEW`
- `booked` starts as `no` and is auto-updated to `yes` after Calendly booking sync (calendly profiles)
- The sheet is the source of truth for both deduplication and conversion rate calculation

---

## Architecture

```
GitHub Actions (daily cron)
  |
  +-- Calendly API  -> count open slots across next 2 weeks
  |                 -> sync past bookings -> mark sheet rows booked=yes
  |
  +-- Google Sheets -> load contacted emails (dedup set)
  |                 -> compute historical booking rate per batch
  |
  +-- BigQuery      -> query users at behavioral milestones
  |                    (filtered by registration date, onboarding status, etc.)
  |
  +-- Dedup filter  -> remove anyone already in the tracking sheet
  |
  +-- Send          -> Gmail SMTP (direct) or ActiveCampaign API (list subscribe)
  |
  +-- Google Sheets -> append new rows: email | batch | booked=no
  |
  +-- Summary email -> stats per batch + error report
```

**Adaptive volume formula** (calendly profiles):
```
emails_to_send = ceil(open_slots / historical_booking_rate)
               = capped at max_emails_per_run
```
Falls back to `emails_per_slot x open_slots` until 30+ sends of history exist for that batch label.

---

## Requirements

```
python >= 3.11
google-cloud-bigquery
google-auth
gspread
pyyaml
requests
```

Install with: `pip install -r requirements.txt`
