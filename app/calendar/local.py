from __future__ import annotations

import hashlib
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import Settings
from app.db import Database

_DAY_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _clock(value: str, fallback: time) -> time:
    try:
        hour, minute = value.split(":", 1)
        return time(int(hour), int(minute))
    except (AttributeError, TypeError, ValueError):
        return fallback


def _preferred_date(value: str, tz: ZoneInfo) -> date | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=tz)
        return parsed.astimezone(tz).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None


class LocalAvailability:
    def __init__(self, settings: Settings, database: Database):
        self.settings = settings
        self.database = database

    def _timezone(self, timezone_name: str) -> tuple[str, ZoneInfo]:
        try:
            return timezone_name, ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError):
            fallback = self.settings.timezone
            try:
                return fallback, ZoneInfo(fallback)
            except ZoneInfoNotFoundError:
                return "UTC", ZoneInfo("UTC")

    def _windows_for_date(self, current_date: date) -> list[tuple[time, time]]:
        config = self.settings.scheduling_config
        hours = config.get("hours", {}) if isinstance(config.get("hours", {}), dict) else {}
        day_config = hours.get(_DAY_NAMES[current_date.weekday()])
        if day_config in (None, False, "disabled"):
            return []
        raw_windows = day_config if isinstance(day_config, list) else [day_config]
        windows: list[tuple[time, time]] = []
        for raw_window in raw_windows:
            if not isinstance(raw_window, dict):
                continue
            start_clock = _clock(str(raw_window.get("start", "09:00")), time(9, 0))
            end_clock = _clock(str(raw_window.get("end", "17:00")), time(17, 0))
            if end_clock > start_clock:
                windows.append((start_clock, end_clock))
        return windows

    def slots(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        config = self.settings.scheduling_config
        timezone_name, tz = self._timezone(
            str(payload.get("timezone") or config.get("timezone") or self.settings.timezone)
        )
        now = datetime.now(tz)
        lead_hours = int(config.get("lead_time_hours", 4))
        duration_minutes = int(config.get("duration_minutes", 90))
        step_minutes = int(config.get("slot_step_minutes", 30))
        days_ahead = int(config.get("days_ahead", 30))
        max_slots = int(config.get("max_results", 8))
        preferred = _preferred_date(str(payload.get("preferred_date") or ""), tz)
        search_start = max(
            now + timedelta(hours=lead_hours),
            datetime.combine(preferred or now.date(), time.min, tzinfo=tz),
        )
        blackout = {str(value) for value in config.get("blackout_dates", [])}

        results: list[dict[str, Any]] = []
        for day_offset in range(days_ahead + 1):
            current_date = search_start.date() + timedelta(days=day_offset)
            if current_date.isoformat() in blackout:
                continue
            for start_clock, end_clock in self._windows_for_date(current_date):
                cursor = datetime.combine(current_date, start_clock, tzinfo=tz)
                window_end = datetime.combine(current_date, end_clock, tzinfo=tz)
                if cursor < search_start:
                    delta_minutes = int((search_start - cursor).total_seconds() // 60)
                    steps = max(0, (delta_minutes + step_minutes - 1) // step_minutes)
                    cursor += timedelta(minutes=steps * step_minutes)
                while cursor + timedelta(minutes=duration_minutes) <= window_end:
                    end = cursor + timedelta(minutes=duration_minutes)
                    start_iso = cursor.astimezone(timezone.utc).isoformat()
                    end_iso = end.astimezone(timezone.utc).isoformat()
                    if not self.database.appointment_conflicts(start_iso, end_iso):
                        slot_id = hashlib.sha256(
                            f"{start_iso}|{end_iso}|{payload.get('service','inspection')}".encode()
                        ).hexdigest()[:20]
                        results.append(
                            {
                                "slot_id": slot_id,
                                "start": cursor.isoformat(),
                                "end": end.isoformat(),
                                "timezone": timezone_name,
                                "display": cursor.strftime("%A, %B %-d at %-I:%M %p"),
                            }
                        )
                        if len(results) >= max_slots:
                            return results
                    cursor += timedelta(minutes=step_minutes)
        return results

    def validate_slot(
        self,
        start_value: str,
        end_value: str,
        timezone_name: str,
        *,
        exclude_id: str = "",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Validate a model-proposed appointment against server-side policy."""
        timezone_name, tz = self._timezone(timezone_name or self.settings.timezone)
        try:
            start = datetime.fromisoformat(str(start_value).replace("Z", "+00:00"))
            end = datetime.fromisoformat(str(end_value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "invalid_appointment_datetime"}
        if start.tzinfo is None:
            start = start.replace(tzinfo=tz)
        else:
            start = start.astimezone(tz)
        if end.tzinfo is None:
            end = end.replace(tzinfo=tz)
        else:
            end = end.astimezone(tz)
        if end <= start:
            return {"ok": False, "error": "appointment_end_must_follow_start"}

        config = self.settings.scheduling_config
        expected_minutes = int(config.get("duration_minutes", 90))
        actual_minutes = int((end - start).total_seconds() // 60)
        if actual_minutes != expected_minutes:
            return {
                "ok": False,
                "error": "invalid_appointment_duration",
                "expected_minutes": expected_minutes,
            }

        local_now = now or datetime.now(tz)
        if local_now.tzinfo is None:
            local_now = local_now.replace(tzinfo=tz)
        else:
            local_now = local_now.astimezone(tz)
        lead_hours = int(config.get("lead_time_hours", 4))
        if start < local_now + timedelta(hours=lead_hours):
            return {"ok": False, "error": "appointment_lead_time_required"}
        days_ahead = int(config.get("days_ahead", 30))
        if start > local_now + timedelta(days=days_ahead):
            return {"ok": False, "error": "appointment_too_far_ahead"}
        blackout = {str(value) for value in config.get("blackout_dates", [])}
        if start.date().isoformat() in blackout:
            return {"ok": False, "error": "appointment_blackout_date"}

        inside_window = False
        for start_clock, end_clock in self._windows_for_date(start.date()):
            window_start = datetime.combine(start.date(), start_clock, tzinfo=tz)
            window_end = datetime.combine(start.date(), end_clock, tzinfo=tz)
            if start >= window_start and end <= window_end:
                inside_window = True
                break
        if not inside_window:
            return {"ok": False, "error": "appointment_outside_business_hours"}

        start_utc = start.astimezone(timezone.utc).isoformat()
        end_utc = end.astimezone(timezone.utc).isoformat()
        if self.database.appointment_conflicts(start_utc, end_utc, exclude_id):
            return {"ok": False, "error": "appointment_slot_conflict"}
        return {
            "ok": True,
            "start": start_utc,
            "end": end_utc,
            "timezone": timezone_name,
        }

    @staticmethod
    def normalize_for_storage(value: str, timezone_name: str) -> str:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo(timezone_name))
        return dt.astimezone(timezone.utc).isoformat()
