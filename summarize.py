"""Build a call summary + outcome from what the agent's tools captured.

Free + deterministic — no extra speech-to-text. The summary is a short recap
clients can scan; the outcome is a single status for filtering/reporting.
"""


def build(state: dict) -> tuple[str, str]:
    parts: list[str] = []
    outcome = "no_outcome"

    lead = state.get("lead") or {}
    if lead:
        who = lead.get("name") or "Caller"
        intent = lead.get("intent") or "enquiry"
        extra = lead.get("notes") or lead.get("area") or ""
        parts.append(f"{who}: {intent}" + (f" — {extra}" if extra else ""))
        outcome = "qualified"

    appt = state.get("appointment") or {}
    if appt:
        parts.append(f"Booked: {appt.get('datetime', 'a time')}")
        outcome = "booked"

    if state.get("transferred"):
        parts.append("Transferred to a human")
        outcome = "transferred"

    summary = " · ".join(parts) if parts else "Call completed — no details captured."
    return summary, outcome
