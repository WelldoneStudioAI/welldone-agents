"""
agents/calendar.py — Agent Google Calendar.
Capacités: créer un événement, lister les prochains événements.
"""
import datetime, logging
from agents._base import BaseAgent
from core.auth import get_google_service
from config import TIMEZONE

log = logging.getLogger(__name__)


class CalendarAgent(BaseAgent):
    name        = "calendar"
    description = "Créer et consulter des événements Google Calendar"

    @property
    def commands(self):
        return {
            "add":  self.add_event,
            "list": self.list_events,
        }

    async def add_event(self, context: dict | None = None) -> str:
        """
        context attendu:
          title (str), date (str "YYYY-MM-DD"), time (str "HH:MM") [optionnel],
          description (str) [optionnel], duration_hours (int) [optionnel, défaut 1]
        """
        ctx = context or {}
        title       = ctx.get("title", "")
        date_str    = ctx.get("date", "")
        time_str    = ctx.get("time")
        description = ctx.get("description", "")
        duration    = int(ctx.get("duration_hours", 1))

        if not title or not date_str:
            return "❌ Paramètres manquants: title et date requis (YYYY-MM-DD)"

        try:
            svc   = get_google_service("calendar", "v3")
            event = {"summary": title, "description": description}

            if time_str:
                dt_start = datetime.datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
                dt_end   = dt_start + datetime.timedelta(hours=duration)
                event["start"] = {"dateTime": dt_start.strftime("%Y-%m-%dT%H:%M:00"), "timeZone": TIMEZONE}
                event["end"]   = {"dateTime": dt_end.strftime("%Y-%m-%dT%H:%M:00"),   "timeZone": TIMEZONE}
                time_display   = f" à {time_str}"
            else:
                end_date = (datetime.datetime.strptime(date_str, "%Y-%m-%d") + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
                event["start"] = {"date": date_str}
                event["end"]   = {"date": end_date}
                time_display   = " (journée)"

            created = svc.events().insert(calendarId="primary", body=event).execute()
            link    = created.get("htmlLink", "")
            log.info(f"calendar.add ok title={title}")
            return f"✅ Événement créé !\n📅 *{title}*\n📆 {date_str}{time_display}\n🔗 {link}"

        except Exception as e:
            log.error(f"calendar.add error: {e}")
            return f"❌ Erreur création événement: {e}"

    async def list_events(self, context: dict | None = None) -> str:
        """
        context: {"days": 7}  (défaut: 7 prochains jours)
        """
        days = int((context or {}).get("days", 7))
        try:
            svc  = get_google_service("calendar", "v3")
            now  = datetime.datetime.utcnow().isoformat() + "Z"
            end  = (datetime.datetime.utcnow() + datetime.timedelta(days=days)).isoformat() + "Z"

            events_result = svc.events().list(
                calendarId="primary",
                timeMin=now,
                timeMax=end,
                maxResults=10,
                singleEvents=True,
                orderBy="startTime",
            ).execute()
            items = events_result.get("items", [])

            if not items:
                return f"📅 Aucun événement dans les {days} prochains jours."

            lines = [f"📅 *Prochains événements ({days}j) :*\n"]
            for e in items:
                start  = e["start"].get("dateTime", e["start"].get("date", ""))[:16]
                title  = e.get("summary", "Sans titre")
                lines.append(f"• {start} — {title}")

            return "\n".join(lines)
        except Exception as e:
            log.error(f"calendar.list error: {e}")
            return f"❌ Erreur lecture calendrier: {e}"


agent = CalendarAgent()
