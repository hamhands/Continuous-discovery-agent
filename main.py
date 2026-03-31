"""
Continuous Discovery Agent
- BigQuery for milestone-based user identification
- Gmail or ActiveCampaign for outbound email
- Calendly for capacity throttling (optional)
- Google Sheets for dedup tracking and conversion rate history
- Adaptive send volume based on historical booking rates
Runs on a schedule via GitHub Actions, emails a summary report after each run.
"""

import os
import json
import math
import yaml
import smtplib
import logging
import traceback
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
from google.cloud import bigquery
from google.oauth2 import service_account
import gspread
import urllib.request

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ── Config from environment ───────────────────────────────────────────────────
CALENDLY_API_KEY      = os.environ["CALENDLY_API_KEY"]

GMAIL_ADDRESS         = os.environ["GMAIL_ADDRESS"]
GMAIL_APP_PASSWORD    = os.environ["GMAIL_APP_PASSWORD"]

BIGQUERY_PROJECT      = os.environ["BIGQUERY_PROJECT"]          # e.g. "my-gcp-project"
BIGQUERY_DATASET      = os.environ.get("BIGQUERY_DATASET", "analytics")

GOOGLE_SA_JSON        = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]

SLACK_WEBHOOK_URL     = os.environ.get("SLACK_WEBHOOK_URL", "")
DRY_RUN               = os.environ.get("DRY_RUN", "false").lower() == "true"
TEST_EMAIL            = os.environ.get("TEST_EMAIL", "").strip()

OUTREACH_PROFILE      = os.environ.get("OUTREACH_PROFILE", "interview_outreach")

# ActiveCampaign credentials — only required for profiles with outbound.type: activecampaign
AC_API_URL            = os.environ.get("ACTIVECAMPAIGN_API_URL", "").rstrip("/")
AC_API_KEY            = os.environ.get("ACTIVECAMPAIGN_API_KEY", "")

# ── Load profile from config.yml ─────────────────────────────────────────────

def load_profile(profile_name):
    config_path = os.path.join(os.path.dirname(__file__), "config.yml")
    with open(config_path) as f:
        config = yaml.safe_load(f)
    profiles = config.get("profiles", {})
    if profile_name not in profiles:
        raise RuntimeError(
            f"Unknown outreach profile '{profile_name}'. "
            f"Available profiles: {sorted(profiles.keys())}"
        )
    log.info("Loaded profile: %s", profile_name)
    return profiles[profile_name]


PROFILE = load_profile(OUTREACH_PROFILE)

# ── Profile-driven constants ──────────────────────────────────────────────────

CAPACITY_TYPE         = PROFILE["capacity"]["type"]          # "calendly" | "fixed"
OUTBOUND_TYPE         = PROFILE.get("outbound", {}).get("type", "gmail")  # "gmail" | "activecampaign"

CALENDLY_EVENT_SLUG   = PROFILE["capacity"].get("event_slug", "") if CAPACITY_TYPE == "calendly" else ""
WEEKLY_MEETING_CAP    = PROFILE["capacity"].get("weekly_meeting_cap", 4) if CAPACITY_TYPE == "calendly" else 0

SHEETS_ID             = PROFILE["sheets_id"]

def _build_signature(sender: dict) -> tuple[str, str]:
    """Build HTML and plain-text signatures from the sender config block."""
    name         = sender["name"]
    title        = sender["title"]
    email        = sender["email"]
    pronouns     = sender.get("pronouns", "")
    company_name = sender.get("company_name", "")
    company_url  = sender.get("company_url", "")
    logo_url     = sender.get("logo_url", "")

    pronouns_html = (
        f' <span style="color: #888888; font-size: 11px; font-weight: normal;">({pronouns})</span>'
        if pronouns else ""
    )
    pronouns_text = f" ({pronouns})" if pronouns else ""

    logo_html = (
        f'<img src="{logo_url}" alt="{company_name}" width="160" style="display:block; margin-bottom: 12px;">'
        if logo_url else ""
    )
    company_link_html = (
        f'<p style="margin: 0;"><a href="{company_url}" style="color: #2eaf7d; font-weight: bold;">{company_name}</a></p>'
        if company_url and company_name else (
            f'<p style="margin: 0;">{company_name}</p>' if company_name else ""
        )
    )

    first_name = name.split()[0]
    sig_html = (
        "<br>"
        f'<div style="font-family: Arial, sans-serif; font-size: 13px; color: #333333; line-height: 1.5;">'
        f'{logo_html}'
        f'<p style="margin: 0 0 4px 0;"><strong style="font-size: 18px;">{name}</strong>{pronouns_html}</p>'
        f'<p style="margin: 0 0 12px 0;">{title}</p>'
        f'<p style="margin: 0 0 4px 0;"><a href="mailto:{email}" style="color: #333333;">{email}</a></p>'
        f'{company_link_html}'
        "</div>"
    )

    sig_text = f"\n\n{first_name}\n\n{name}{pronouns_text}\n{title}\n{email}"
    if company_name and company_url:
        sig_text += f"\n{company_url}"
    elif company_name:
        sig_text += f"\n{company_name}"

    return sig_html, sig_text


EMAIL_SIGNATURE_HTML, EMAIL_SIGNATURE_TEXT = _build_signature(PROFILE["sender"])

# Build TEMPLATES and BATCH_QUERIES from the profile's batches list.
# Templates dict keys are the batch label (e.g. "OB", "FT").
TEMPLATES = {}
BATCH_QUERIES = {}
for _batch in PROFILE["batches"]:
    _label = _batch["label"]
    TEMPLATES[_label] = {
        # gmail outbound fields (not used for activecampaign)
        "subject":            _batch.get("subject", ""),
        "body_text":          _batch.get("body_text", "").rstrip("\n"),
        "body_html":          _batch.get("body_html", "").rstrip("\n"),
        # activecampaign outbound field (not used for gmail)
        "list_id":            _batch.get("list_id"),
        # volume controls
        "limit":              _batch.get("emails_per_slot", 0),  # calendly capacity only
        "max_emails_per_run": _batch["max_emails_per_run"],
        "label":              _label,
    }
    BATCH_QUERIES[_label] = _batch["query"]


# ── Calendly helpers ──────────────────────────────────────────────────────────

def calendly_get(path, params=None):
    url = path if path.startswith("https://") else f"https://api.calendly.com/{path}"
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {CALENDLY_API_KEY}"},
        params=params,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def get_calendly_uris():
    data = calendly_get("users/me")
    user_uri = data["resource"]["uri"]
    org_uri  = data["resource"]["current_organization"]
    return user_uri, org_uri


def get_event_type_uri(user_uri):
    """Find the URI for our specific event type by slug (queried by user scope)."""
    page_token = None
    while True:
        params = {"user": user_uri, "count": 100}
        if page_token:
            params["page_token"] = page_token
        data = calendly_get("event_types", params)
        for et in data.get("collection", []):
            if CALENDLY_EVENT_SLUG in et.get("scheduling_url", ""):
                return et["uri"]
        next_page = data.get("pagination", {}).get("next_page_token")
        if not next_page:
            break
        page_token = next_page
    raise RuntimeError(f"Event type not found for slug: {CALENDLY_EVENT_SLUG}")


def count_remaining_slots(org_uri, event_type_uri):
    """
    For each of the next 2 ISO weeks, count active scheduled events
    against the WEEKLY_MEETING_CAP and return total remaining slots.
    """
    now = datetime.now(timezone.utc)
    total_remaining = 0

    for week_offset in range(2):
        # Start of the target week (Monday 00:00 UTC)
        days_to_monday = now.weekday()
        week_start = (now - timedelta(days=days_to_monday) + timedelta(weeks=week_offset)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        week_end = week_start + timedelta(days=7)

        next_url = None
        booked = 0
        while True:
            if next_url:
                data = calendly_get(next_url)
            else:
                data = calendly_get("scheduled_events", {
                    "organization":   org_uri,
                    "status":         "active",
                    "min_start_time": week_start.isoformat(),
                    "max_start_time": week_end.isoformat(),
                    "count":          100,
                })
            for ev in data.get("collection", []):
                if ev.get("event_type") == event_type_uri:
                    booked += 1
            next_url = data.get("pagination", {}).get("next_page")
            if not next_url:
                break
        remaining = max(0, WEEKLY_MEETING_CAP - booked)
        log.info(
            "Week %s (%s): %d booked, %d remaining",
            week_offset + 1,
            week_start.strftime("%Y-%m-%d"),
            booked,
            remaining,
        )
        total_remaining += remaining

    return total_remaining


def get_booking_link():
    return f"https://calendly.com/d/{CALENDLY_EVENT_SLUG}"


# ── BigQuery helpers ──────────────────────────────────────────────────────────

def get_bq_client():
    sa_info = json.loads(GOOGLE_SA_JSON)
    creds = service_account.Credentials.from_service_account_info(
        sa_info,
        scopes=["https://www.googleapis.com/auth/bigquery"],
    )
    return bigquery.Client(project=BIGQUERY_PROJECT, credentials=creds)


def run_bq_query(client, sql):
    job = client.query(sql)
    rows = list(job.result())
    return [dict(r) for r in rows]


# ── Google Sheets dedup ───────────────────────────────────────────────────────

def get_sheets_client():
    sa_info = json.loads(GOOGLE_SA_JSON)
    creds = service_account.Credentials.from_service_account_info(
        sa_info,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive.readonly",
        ],
    )
    return gspread.authorize(creds)


def load_sheet_data(gc):
    """
    Returns:
      contacted  — set of lowercased emails already reached out to (for dedup)
      records    — list of dicts with keys email, batch, booked ("yes"/"no")
                   used to compute historical conversion rates
    """
    sh = gc.open_by_key(SHEETS_ID)
    ws = sh.get_worksheet(0)
    all_rows = ws.get_all_values()
    contacted = set()
    records = []
    for row in all_rows[1:]:  # skip header
        if not row or not row[0]:
            continue
        email  = row[0].strip().lower()
        batch  = row[1].strip() if len(row) > 1 else ""
        booked = row[2].strip().lower() if len(row) > 2 else "no"
        contacted.add(email)
        records.append({"email": email, "batch": batch, "booked": booked})
    log.info("Loaded %d previously contacted emails from sheet", len(contacted))
    return contacted, records


def compute_conversion_rate(records, batch_label, min_samples=30):
    """
    Estimate booking conversion rate for a batch from sheet history.
    Matches rows whose batch field ends with ' <label>' (e.g. '032526 OB').
    Returns float (e.g. 0.03) or None if there are fewer than min_samples rows.
    """
    batch_rows = [r for r in records if r["batch"].endswith(f" {batch_label}")]
    total = len(batch_rows)
    if total < min_samples:
        log.info(
            "[%s] Too few historical samples for adaptive rate (%d < %d) — using fallback",
            batch_label, total, min_samples,
        )
        return None
    booked = sum(1 for r in batch_rows if r["booked"] == "yes")
    rate = booked / total
    log.info(
        "[%s] Historical conversion rate: %.1f%% (%d booked / %d sent)",
        batch_label, rate * 100, booked, total,
    )
    return rate


def adaptive_limit(slots, conversion_rate, fallback_per_slot, max_per_run):
    """
    Calculate how many emails to send to probabilistically fill `slots` interviews.

    When conversion_rate is known: ceil(slots / rate), capped at max_per_run.
    When conversion_rate is None:  fallback_per_slot * slots, capped at max_per_run.
    """
    if conversion_rate and conversion_rate > 0:
        needed = math.ceil(slots / conversion_rate)
        log.info(
            "Adaptive limit: ceil(%d slots / %.1f%%) = %d emails (cap %d)",
            slots, conversion_rate * 100, needed, max_per_run,
        )
    else:
        needed = fallback_per_slot * slots
        log.info(
            "Fallback limit: %d/slot x %d slots = %d emails (cap %d)",
            fallback_per_slot, slots, needed, max_per_run,
        )
    return min(needed, max_per_run)


def sync_bookings_from_calendly(gc, org_uri, event_type_uri):
    """
    Fetch all active Calendly invitees for this event type (past 60 days),
    match their emails against the tracking sheet, and flip booked -> "yes"
    for any rows still marked "no". Returns the number of rows updated.
    """
    min_time = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()

    # 1. Collect UUIDs of all scheduled events for this event type
    event_uuids = []
    next_url = None
    while True:
        if next_url:
            data = calendly_get(next_url)
        else:
            data = calendly_get("scheduled_events", {
                "organization":   org_uri,
                "status":         "active",
                "min_start_time": min_time,
                "count":          100,
            })
        for ev in data.get("collection", []):
            if ev.get("event_type") == event_type_uri:
                event_uuids.append(ev["uri"].split("/")[-1])
        next_url = data.get("pagination", {}).get("next_page")
        if not next_url:
            break

    log.info("Calendly sync: found %d scheduled events to check", len(event_uuids))

    # 2. Collect every invitee email across those events
    booked_emails = set()
    for uuid in event_uuids:
        next_url = None
        while True:
            if next_url:
                data = calendly_get(next_url)
            else:
                data = calendly_get(f"scheduled_events/{uuid}/invitees", {"count": 100})
            for inv in data.get("collection", []):
                email = inv.get("email", "").strip().lower()
                if email:
                    booked_emails.add(email)
            next_url = data.get("pagination", {}).get("next_page")
            if not next_url:
                break

    log.info("Calendly sync: %d unique booked emails found", len(booked_emails))

    if not booked_emails:
        return 0

    # 3. Find sheet rows where the email is booked but still marked "no"
    sh = gc.open_by_key(SHEETS_ID)
    ws = sh.get_worksheet(0)
    all_rows = ws.get_all_values()

    updates = []
    for i, row in enumerate(all_rows[1:], start=2):  # row 1 is header; gspread is 1-indexed
        if not row or not row[0]:
            continue
        email  = row[0].strip().lower()
        booked = row[2].strip().lower() if len(row) > 2 else "no"
        if email in booked_emails and booked != "yes":
            updates.append({"range": f"C{i}", "values": [["yes"]]})

    if updates:
        ws.batch_update(updates, value_input_option="RAW")
        log.info("Calendly sync: marked %d rows as booked", len(updates))
    else:
        log.info("Calendly sync: no new bookings to update")

    return len(updates)


def append_to_sheet(gc, rows):
    """rows: list of [email, batch_label]"""
    if not rows:
        return
    sh = gc.open_by_key(SHEETS_ID)
    ws = sh.get_worksheet(0)
    ws.append_rows(rows, value_input_option="RAW")
    log.info("Appended %d rows to tracking sheet", len(rows))


# ── Gmail sending ─────────────────────────────────────────────────────────────

def build_message(to_email, subject, body_text, body_html):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_ADDRESS
    msg["To"]      = to_email

    full_text = body_text + EMAIL_SIGNATURE_TEXT
    full_html = body_html + EMAIL_SIGNATURE_HTML

    msg.attach(MIMEText(full_text, "plain"))
    msg.attach(MIMEText(full_html, "html"))
    return msg


def send_emails(recipients, template, booking_link, batch_label):
    sent, failed = 0, []

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        for r in recipients:
            email = r["email"]
            subject   = template["subject"]
            body_text = template["body_text"].format(calendly_link=booking_link)
            body_html = template["body_html"].format(calendly_link=booking_link)
            msg = build_message(email, subject, body_text, body_html)
            try:
                server.sendmail(GMAIL_ADDRESS, email, msg.as_string())
                sent += 1
                log.info("[%s] Sent -> %s", batch_label, email)
            except Exception as exc:
                failed.append(email)
                log.error("[%s] Failed -> %s: %s", batch_label, email, exc)

    return sent, failed


def add_to_activecampaign(recipients, list_id, batch_label):
    """
    Upsert each recipient as an AC contact then subscribe them to list_id.
    AC handles the actual email send via whatever automation is attached to that list.
    Returns (added_count, failed_emails).
    """
    if not AC_API_URL or not AC_API_KEY:
        raise RuntimeError(
            "ACTIVECAMPAIGN_API_URL and ACTIVECAMPAIGN_API_KEY must be set "
            "for profiles with outbound.type: activecampaign"
        )

    headers = {"Api-Token": AC_API_KEY, "Content-Type": "application/json"}
    added, failed = 0, []

    for r in recipients:
        email = r["email"]
        try:
            # 1. Upsert contact (creates if new, updates if existing)
            sync_resp = requests.post(
                f"{AC_API_URL}/api/3/contact/sync",
                json={"contact": {"email": email}},
                headers=headers,
                timeout=15,
            )
            sync_resp.raise_for_status()
            contact_id = sync_resp.json()["contact"]["id"]

            # 2. Subscribe contact to the target list (status 1 = active/subscribed)
            list_resp = requests.post(
                f"{AC_API_URL}/api/3/contactLists",
                json={"contactList": {"list": list_id, "contact": contact_id, "status": 1}},
                headers=headers,
                timeout=15,
            )
            list_resp.raise_for_status()
            added += 1
            log.info("[%s] Added to AC list %s -> %s", batch_label, list_id, email)
        except Exception as exc:
            failed.append(email)
            log.error("[%s] AC failed -> %s: %s", batch_label, email, exc)

    return added, failed


# ── Slack / summary ───────────────────────────────────────────────────────────

def slack_alert(message):
    if not SLACK_WEBHOOK_URL:
        return
    try:
        payload = json.dumps({"text": message}).encode()
        req = urllib.request.Request(
            SLACK_WEBHOOK_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as exc:
        log.warning("Slack alert failed: %s", exc)


def send_summary_email(stats, errors, bookings_synced=0):
    if not stats and not errors:
        return
    ok = not errors and all(b["send_failed"] == 0 for b in stats.values())
    icon = "OK" if ok else "WARN"
    subject = f"[{icon}] Outreach Run -- {datetime.now().strftime('%Y-%m-%d')} [{OUTREACH_PROFILE}]"

    lines = [f"<h2>{icon} Daily Outreach Summary</h2>"]
    lines.append(f"<p><b>Date:</b> {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}</p>")
    lines.append(f"<p><b>Profile:</b> {OUTREACH_PROFILE}</p>")
    lines.append(f"<p><b>Capacity:</b> {CAPACITY_TYPE} | <b>Outbound:</b> {OUTBOUND_TYPE}</p>")
    lines.append(f"<p><b>Bookings synced from Calendly:</b> {bookings_synced}</p>")
    lines.append(f"<p><b>Dry run:</b> {DRY_RUN}</p>")

    for label, s in stats.items():
        rate = s["conversion_rate"]
        rate_str = f"{rate * 100:.1f}% (historical)" if rate is not None else "fallback (insufficient history)"
        lines.append(f"<h3>Batch {label}</h3><ul>")
        lines.append(f"<li>Slots this run: {s['slots']}</li>")
        lines.append(f"<li>Conversion rate used: {rate_str}</li>")
        lines.append(f"<li>Queried: {s['queried']}</li>")
        lines.append(f"<li>After dedup: {s['after_dedup']}</li>")
        lines.append(f"<li>Sent: {s['sent']}</li>")
        lines.append(f"<li>Failed: {s['send_failed']}</li>")
        lines.append("</ul>")

    if errors:
        lines.append("<h3>Errors</h3><ul>")
        for e in errors:
            lines.append(f"<li>{e}</li>")
        lines.append("</ul>")

    body_html = "\n".join(lines)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_ADDRESS
    msg["To"]      = GMAIL_ADDRESS
    msg.attach(MIMEText(body_html, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_ADDRESS, GMAIL_ADDRESS, msg.as_string())
        log.info("Summary email sent to %s", GMAIL_ADDRESS)
    except Exception as exc:
        log.error("Summary email failed: %s", exc)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    errors = []
    stats  = {}
    new_sheet_rows  = []
    bookings_synced = 0

    try:
        # 1. Capacity check
        event_type_uri = None
        booking_link   = None
        total_slots    = None

        if CAPACITY_TYPE == "calendly":
            log.info("Checking Calendly availability...")
            user_uri, org_uri = get_calendly_uris()
            event_type_uri    = get_event_type_uri(user_uri)
            total_slots       = count_remaining_slots(org_uri, event_type_uri)
            booking_link      = get_booking_link()
            log.info("Total remaining slots (next 2 weeks): %d", total_slots)
            if total_slots == 0:
                log.info("No open slots — running booking sync then exiting.")
                gc = get_sheets_client()
                bookings_synced = sync_bookings_from_calendly(gc, org_uri, event_type_uri)
                send_summary_email({}, [])
                return
        else:
            log.info("Capacity type: fixed — skipping Calendly check")

        # 2. BigQuery + Google Sheets clients
        log.info("Connecting to BigQuery and Google Sheets...")
        bq_client = get_bq_client()
        gc        = get_sheets_client()

        # Sync Calendly bookings -> sheet (calendly capacity only)
        if CAPACITY_TYPE == "calendly":
            bookings_synced = sync_bookings_from_calendly(gc, org_uri, event_type_uri)
        else:
            bookings_synced = 0

        contacted, sheet_records = load_sheet_data(gc)

        # 3. Process each batch
        run_contacted = set()  # dedup within this run across batches

        for label, template in TEMPLATES.items():
            if CAPACITY_TYPE == "calendly":
                rate        = compute_conversion_rate(sheet_records, label)
                total_limit = adaptive_limit(
                    slots=total_slots,
                    conversion_rate=rate,
                    fallback_per_slot=template["limit"],
                    max_per_run=template["max_emails_per_run"],
                )
            else:
                rate        = None
                total_limit = template["max_emails_per_run"]

            log.info("[%s] Querying BigQuery for up to %d users...", label, total_limit)
            sql = BATCH_QUERIES[label].format(
                project=BIGQUERY_PROJECT,
                dataset=BIGQUERY_DATASET,
                limit=total_limit,
            )
            rows = run_bq_query(bq_client, sql)
            queried = len(rows)
            log.info("[%s] Got %d rows from BigQuery", label, queried)

            # Dedup against sheet + this run (skipped for test sends)
            if TEST_EMAIL:
                recipients = [{"email": TEST_EMAIL}]
                log.info("[%s] TEST MODE — sending only to %s", label, TEST_EMAIL)
            else:
                recipients = [
                    r for r in rows
                    if r["email"].lower() not in contacted
                    and r["email"].lower() not in run_contacted
                ]
            after_dedup = len(recipients)
            log.info("[%s] %d after dedup (removed %d)", label, after_dedup, queried - after_dedup)

            sent_count, failed_emails = 0, []
            if not DRY_RUN or TEST_EMAIL:
                if OUTBOUND_TYPE == "activecampaign":
                    sent_count, failed_emails = add_to_activecampaign(
                        recipients, template["list_id"], label
                    )
                else:
                    sent_count, failed_emails = send_emails(
                        recipients, template, booking_link, label
                    )
            else:
                log.info("[%s] DRY RUN — would send to %d recipients", label, after_dedup)
                sent_count = after_dedup

            # Track newly emailed addresses
            today_str = datetime.now().strftime("%m%d%y")
            for r in recipients:
                run_contacted.add(r["email"].lower())
                if not DRY_RUN:
                    new_sheet_rows.append([r["email"], f"{today_str} {label}", "no"])

            if failed_emails:
                errors.append(f"[{label}] Failed to send to: {', '.join(failed_emails)}")

            stats[label] = {
                "slots":           total_slots,
                "conversion_rate": rate,
                "queried":         queried,
                "after_dedup":     after_dedup,
                "sent":            sent_count,
                "send_failed":     len(failed_emails),
            }

        # 4. Append to tracking sheet
        if new_sheet_rows and not DRY_RUN:
            append_to_sheet(gc, new_sheet_rows)

    except Exception as exc:
        tb = traceback.format_exc()
        errors.append(f"Fatal error: {exc}\n{tb}")
        log.error("Fatal error:\n%s", tb)
        slack_alert(f":rotating_light: Outreach run failed:\n```{exc}```")

    # 5. Summary
    if errors:
        slack_alert(":warning: Outreach completed with errors:\n" + "\n".join(errors))

    send_summary_email(stats, errors, bookings_synced)

    if errors:
        raise SystemExit(1)
    log.info("Run complete. Stats: %s", stats)


if __name__ == "__main__":
    main()
