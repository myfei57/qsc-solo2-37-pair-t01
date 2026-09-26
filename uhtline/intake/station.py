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

RECEIPT_LIMIT = 256


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
        self._receipts: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        stored = self.store.try_read(self.document)
        if stored is None:
            return
        self._total_litres = float(stored.payload.get("total_litres", 0.0))
        receipts = stored.payload.get("receipts")
        if receipts is None:
            # Backwards compatibility with documents written by the single-receipt build.
            last = stored.payload.get("last_receipt")
            self._receipts = [] if last is None else [dict(last)]
        else:
            self._receipts = [dict(item) for item in receipts]

    def persist(self) -> None:
        self.store.write(
            self.document,
            {"total_litres": self._total_litres, "receipts": self._receipts[-RECEIPT_LIMIT:]},
        )

    def screen(self, volume_litres: float, temperature_c: float) -> dict[str, Any]:
        """Report whether a delivery would be accepted, without changing state."""

        envelope = self.config.throughput
        reasons: list[str] = []
        try:
            volume = float(volume_litres)
        except (TypeError, ValueError):
            volume = None
            reasons.append("volume is not a number")
        else:
            if volume < envelope.intake_minimum_litres or volume > envelope.intake_maximum_litres:
                reasons.append(
                    f"volume {volume:g} L is outside the intake window "
                    f"[{envelope.intake_minimum_litres:g}, {envelope.intake_maximum_litres:g}] L"
                )
        try:
            temperature = float(temperature_c)
        except (TypeError, ValueError):
            temperature = None
            reasons.append("temperature is not a number")
        else:
            if temperature < envelope.intake_minimum_c or temperature > envelope.intake_maximum_c:
                reasons.append(
                    f"temperature {temperature:g} C is outside the cold-chain window "
                    f"[{envelope.intake_minimum_c:g}, {envelope.intake_maximum_c:g}] C"
                )
        return {
            "accepted": not reasons,
            "reasons": reasons,
            "volume_litres": volume,
            "temperature_c": temperature,
            "window_litres": [envelope.intake_minimum_litres, envelope.intake_maximum_litres],
            "window_c": [envelope.intake_minimum_c, envelope.intake_maximum_c],
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
        envelope = self.config.throughput
        volume = require_within(
            volume_litres,
            envelope.intake_minimum_litres,
            envelope.intake_maximum_litres,
            field_name="volume_litres",
            scope="throughput",
        )
        temperature = require_within(
            temperature_c,
            envelope.intake_minimum_c,
            envelope.intake_maximum_c,
            field_name="temperature_c",
            scope="throughput",
        )
        batch = validate_token(batch_id, field_name="batch id")
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
        self._receipts.append(receipt)
        if len(self._receipts) > RECEIPT_LIMIT:
            self._receipts = self._receipts[-RECEIPT_LIMIT:]
        self.persist()
        self.audit.record("intake", batch, f"{volume:g} L accepted", cause=None)
        return {"receipt": receipt, "staged": record.as_dict(), "total_litres": self._total_litres}

    def require_receipt(self, batch_id: str) -> dict[str, Any]:
        """Return the latest receipt recorded for the batch, never another batch's."""

        label = str(batch_id)
        for receipt in reversed(self._receipts):
            if receipt["batch_id"] == label:
                return dict(receipt)
        raise StateError("no intake receipt has been recorded for this batch", batch=label)

    def receipts(self, *, batch_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        selected = self._receipts
        if batch_id is not None:
            selected = [receipt for receipt in selected if receipt["batch_id"] == str(batch_id)]
        return [dict(receipt) for receipt in selected[-max(0, int(limit)) :]]

    def batch_litres(self, batch_id: str) -> float:
        return round(
            sum(receipt["volume_litres"] for receipt in self._receipts if receipt["batch_id"] == str(batch_id)),
            4,
        )

    def total_litres(self) -> float:
        return self._total_litres

    def snapshot(self) -> dict[str, Any]:
        return {
            "total_litres": self._total_litres,
            "receipts": len(self._receipts),
            "recent": [dict(receipt) for receipt in self._receipts[-5:]],
            "latest": None if not self._receipts else dict(self._receipts[-1]),
        }


__all__ = ["RECEIPT_LIMIT", "IntakeStation"]
