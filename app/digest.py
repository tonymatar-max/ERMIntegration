"""Weekly delivery digest — a management summary email built from the current
cache (same figures as the Analysis page) and sent to users who have an email.
Also used for the admin "Send digest now" button.
"""

from . import analysis, auth, mailer, pp_report, resource_planning, settings_store
from .logging_config import logger


def _firmwide_data():
    """(analysis dict, fetched_on) over the whole portfolio, or (None, None)
    if there's no cached data yet."""
    projects, fetched_on = settings_store.load_cache("projects")
    timespent, _ = settings_store.load_cache("timespent")
    if not projects:
        return None, None
    ts = list(timespent or []) + pp_report.ksa_timesheet_entries_all(
        settings_store.distinct_timesheet_users(timespent or []))
    report_month = (fetched_on or "")[:7] or resource_planning.current_month_str()
    return analysis.build_analysis(projects, ts, report_month, settings_store.get_ksa_excluded_codes()), fetched_on


def recipients() -> list:
    """Every user with an email address."""
    return [u["email"].strip() for u in auth.list_users() if (u.get("email") or "").strip()]


def _compose(data, fetched_on):
    p = data["portfolio"]
    last_m = data["months"][-1]
    util = data["utilization"][last_m]
    over = data["top_over_budget"][:5]
    window = f"{data['month_labels'][0]}–{data['month_labels'][-1]}"
    subject = f"ERM delivery digest — {data['month_labels'][-1]} (util {util['avg_pct']}%, {p['over_budget']} over budget)"

    lines = [
        f"ERM Project Ledger — delivery digest ({window})",
        f"As of {(fetched_on or '')[:10]}",
        "",
        f"Hours logged ({window}): {p['window_hours']:,}",
        f"Utilization ({data['month_labels'][-1]}): {util['avg_pct']}%  ({util['active']} active consultants)",
        f"Order book: ${p['order']:,}   Budget consumed: {p['consumed_pct']}%",
        f"Over budget: {p['over_budget']} projects   Negative remaining: {p['negative_remaining']}",
        "",
        "Top over-budget projects:",
    ]
    for r in over:
        lines.append(f"  • {r['name'][:48]} ({r['country'] or '—'}, {r['pm'] or '—'}): {r['spent']:,}/{r['est']:,}h = {r['pct']}% (+{r['overrun']:,}h)")
    lines.append("")
    lines.append("Open the Analysis page for the full picture.")
    text = "\n".join(lines)

    rows_html = "".join(
        f"<tr><td style='padding:4px 8px'>{r['name'][:48]}</td><td style='padding:4px 8px;color:#6b7280'>{r['country'] or '—'}</td>"
        f"<td style='padding:4px 8px;text-align:right'>{r['spent']:,}/{r['est']:,}</td>"
        f"<td style='padding:4px 8px;text-align:right;color:#b3261e;font-weight:600'>+{r['overrun']:,}</td>"
        f"<td style='padding:4px 8px;text-align:right'>{r['pct']}%</td></tr>"
        for r in over
    )
    html = f"""<div style="font-family:Segoe UI,Arial,sans-serif;color:#141a2e;max-width:640px">
  <h2 style="color:#07153a;margin:0 0 2px">ERM delivery digest</h2>
  <p style="color:#6b7280;margin:0 0 16px">{window} · as of {(fetched_on or '')[:10]}</p>
  <table style="border-collapse:collapse;width:100%;font-size:14px">
    <tr><td style="padding:6px 8px">Hours logged ({window})</td><td style="padding:6px 8px;text-align:right;font-weight:600">{p['window_hours']:,}</td></tr>
    <tr><td style="padding:6px 8px">Utilization ({data['month_labels'][-1]})</td><td style="padding:6px 8px;text-align:right;font-weight:600">{util['avg_pct']}% · {util['active']} active</td></tr>
    <tr><td style="padding:6px 8px">Order book</td><td style="padding:6px 8px;text-align:right;font-weight:600">${p['order']:,}</td></tr>
    <tr><td style="padding:6px 8px">Over budget / negative remaining</td><td style="padding:6px 8px;text-align:right;font-weight:600">{p['over_budget']} / {p['negative_remaining']}</td></tr>
  </table>
  <h3 style="color:#07153a;margin:18px 0 6px;font-size:15px">Top over-budget projects</h3>
  <table style="border-collapse:collapse;width:100%;font-size:13px">
    <tr style="color:#6b7280;text-align:left"><th style="padding:4px 8px">Project</th><th style="padding:4px 8px">Country</th><th style="padding:4px 8px;text-align:right">Spent/Est</th><th style="padding:4px 8px;text-align:right">Overrun</th><th style="padding:4px 8px;text-align:right">%</th></tr>
    {rows_html}
  </table>
</div>"""
    return subject, text, html


def send_digest(to=None) -> dict:
    """Build and send the digest. `to` overrides recipients (for a test).
    Returns {sent:int, recipients:[...]} or raises mailer.MailError /
    ValueError with a user-facing message."""
    data, fetched_on = _firmwide_data()
    if not data:
        raise ValueError("No cached data yet — run Refresh from Redmine first.")
    rcpts = [to] if to else recipients()
    rcpts = [r for r in rcpts if r]
    if not rcpts:
        raise ValueError("No recipients — set email addresses for users (Users / Settings) first.")
    subject, text, html = _compose(data, fetched_on)
    mailer.send_email(rcpts, subject, text, html=html)
    logger.info("Digest sent to %d recipient(s)", len(rcpts))
    return {"sent": len(rcpts), "recipients": rcpts}
