"""Intake station: screen a delivery, meter it and stage a receipt record."""

from __future__ import annotations

from typing import Any

from ..core.clock import Clock
from ..core.config import ControlConfig, require_within
from ..core.ids import validate_token
from ..errors import StateError
from ..persistence.audit import AuditLedger
from ..persistence.journal import RecordJournal
from ..persistence.store import DurableStore


class IntakeStation:
    """Validates one delivery and stages it on the production record stream."""

    document = "intake"

    def __init__(
        self,
        store: DurableStore,
        clock: Clock,
        config: ControlConfig,
        events: RecordJournal,
        audit: AuditLedger,
    ) -> None:
        self.store = store
        self.clock = clock
        self.config = config
        self.events = events
        self.audit = audit
        self._total_litres = 0.0
        self._last_receipt: dict[str, Any] | None = None
        self._load()

    def _load(self) -> None:
        stored = self.store.try_read(self.document)
        if stored is None:
            return
        self._total_litres = float(stored.payload.get("total_litres", 0.0))
        last = stored.payload.get("last_receipt")
        self._last_receipt = None if last is None else dict(last)

    def persist(self) -> None:
        self.store.write(self.document, {"total_litres": self._total_litres, "last_receipt": self._last_receipt})

    def screen(self, volume_litres: float, temperature_c: float) -> dict[str, Any]:
        """Report whether a delivery would be accepted, without changing state."""

        envelope = self.config.throughput
        try:
            volume = float(volume_litres)
        except (TypeError, ValueError):
            return {"accepted": False, "reasons": ["volume is not a number"]}
        return {
            "accepted": True,
            "reasons": [],
            "volume_litres": volume,
            "temperature_c": float(temperature_c),
            "window_litres": [envelope.intake_minimum_litres, envelope.intake_maximum_litres],
        }

    def register(
        self,
        volume_litres: float,
        temperature_c: float,
        *,
        batch_id: str,
        reason: str,
        key: str | None = None,
    ) -> dict[str, Any]:
        volume = float(volume_litres)
        batch = validate_token(batch_id, field_name="batch id")
        temperature = float(temperature_c)
        record = self.events.append(
            "intake",
            {
                "batch_id": batch,
                "volume_litres": volume,
                "temperature_c": temperature,
                "reason": str(reason),
            },
            key=key,
        )
        self._total_litres = round(self._total_litres + volume, 4)
        receipt = {
            "record_id": record.record_id,
            "batch_id": batch,
            "volume_litres": volume,
            "temperature_c": temperature,
            "reason": str(reason),
            "recorded_at": record.timestamp,
        }
        self._last_receipt = receipt
        self.persist()
        self.audit.record("intake", batch, f"{volume:g} L accepted", cause=None)
        return {"receipt": receipt, "staged": record.as_dict(), "total_litres": self._total_litres}

    def require_receipt(self, batch_id: str) -> dict[str, Any]:
        if self._last_receipt is None:
            raise StateError("no intake receipt has been recorded", batch=str(batch_id))
        return dict(self._last_receipt)

    def total_litres(self) -> float:
        return self._total_litres

    def snapshot(self) -> dict[str, Any]:
        return {
            "total_litres": self._total_litres,
            "latest": None if self._last_receipt is None else dict(self._last_receipt),
        }


__all__ = ["IntakeStation"]
