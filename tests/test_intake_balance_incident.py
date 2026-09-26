"""Regression coverage for the early-shift, two-truck intake incident.

The paper ledgers lost the first truck's receipt and the tank's standing stock
once the second truck weighed in, and a delivery was released against a tank
that was treated as full even though it was not. The level must always be the
standing stock plus the new delivery, receipts must be retrievable per batch,
and any charge that would exceed capacity must be refused.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from uhtline.app.runtime import build_runtime
from uhtline.core.clock import ManualClock
from uhtline.core.config import fast_test_config
from uhtline.errors import RangeError, StateError

from .support import manual_runtime


def _open_intake(tmp_path: Path):
    runtime = manual_runtime(tmp_path)
    runtime.control.open_batch("B-0501", "milk", reason="morning shift")
    runtime.control.start_intake(reason="morning shift")
    return runtime


def test_the_first_trucks_receipt_survives_the_second_truck(tmp_path: Path) -> None:
    runtime = _open_intake(tmp_path)
    first = runtime.control.receive(700.0, 6.0, batch_id="B-0501", reason="truck 1", key="truck-1")
    runtime.control.commit_records()
    runtime.control.receive(800.0, 6.0, batch_id="B-0501", reason="truck 2", key="truck-2")

    receipts = runtime.intake.receipts(batch_id="B-0501")
    assert [receipt["record_id"] for receipt in receipts] == [
        first["receipt"]["record_id"],
        "LINEEV-00002",
    ]
    assert [receipt["reason"] for receipt in receipts] == ["truck 1", "truck 2"]
    # The latest receipt for the batch is the second truck, never another batch's.
    assert runtime.intake.require_receipt("B-0501")["record_id"] == "LINEEV-00002"
    assert runtime.intake.total_litres() == 1500.0
    assert runtime.intake.batch_litres("B-0501") == 1500.0
    assert runtime.intake.snapshot()["receipts"] == 2
    assert [receipt["volume_litres"] for receipt in runtime.intake.snapshot()["recent"]] == [700.0, 800.0]


def test_require_receipt_never_returns_another_batchs_receipt(tmp_path: Path) -> None:
    runtime = _open_intake(tmp_path)
    runtime.control.receive(700.0, 6.0, batch_id="B-0501", reason="truck 1", key="truck-1")
    with pytest.raises(StateError):
        runtime.intake.require_receipt("B-9999")


def test_a_partly_full_tank_accumulates_instead_of_being_treated_as_full(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    runtime.balance.charge(600.0, reason="truck 1")
    runtime.balance.charge(600.0, reason="truck 2")
    assert runtime.balance.level_litres() == 1200.0
    assert runtime.balance.capacity_litres() == 1500.0
    assert runtime.balance.free_litres() == 300.0
    actions = [entry["action"] for entry in runtime.balance.history()]
    assert actions == ["charge", "charge"]
    assert [entry["level_litres"] for entry in runtime.balance.history()] == [600.0, 1200.0]


def test_a_charge_that_would_exceed_capacity_is_refused_against_standing_stock(
    tmp_path: Path,
) -> None:
    runtime = manual_runtime(tmp_path)
    runtime.balance.charge(900.0, reason="truck 1")
    with pytest.raises(RangeError) as failure:
        runtime.balance.charge(900.0, reason="truck 2")
    assert failure.value.details["level_litres"] == 900.0
    assert failure.value.details["projected_litres"] == 1800.0
    assert failure.value.details["capacity_litres"] == 1500.0
    # The refused charge must not move the stock.
    assert runtime.balance.level_litres() == 900.0


def test_draining_reduces_the_level_and_over_drain_is_refused(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    runtime.balance.charge(600.0, reason="truck 1")
    runtime.balance.drain(200.0, reason="downstream draw")
    assert runtime.balance.level_litres() == 400.0
    assert runtime.balance.is_charging() is True
    with pytest.raises(RangeError):
        runtime.balance.drain(700.0, reason="over draw")
    # Drain the remainder: the tank is empty and no longer charging.
    runtime.balance.drain(400.0, reason="last draw")
    assert runtime.balance.level_litres() == 0.0
    assert runtime.balance.is_charging() is False


def test_intake_and_balance_ledgers_survive_a_restart(tmp_path: Path) -> None:
    first = build_runtime(fast_test_config(), tmp_path, ManualClock())
    first.control.open_batch("B-0501", "milk", reason="morning shift")
    first.control.start_intake(reason="morning shift")
    first.control.receive(700.0, 6.0, batch_id="B-0501", reason="truck 1", key="truck-1")
    first.control.commit_records()
    first.control.receive(800.0, 6.0, batch_id="B-0501", reason="truck 2", key="truck-2")
    first.control.charge_balance(600.0, reason="first charge")
    first.balance.drain(100.0, reason="draw")
    first.control.persist_all()

    second = build_runtime(fast_test_config(), tmp_path, ManualClock())
    assert second.intake.total_litres() == 1500.0
    assert [receipt["reason"] for receipt in second.intake.receipts(batch_id="B-0501")] == [
        "truck 1",
        "truck 2",
    ]
    assert second.balance.level_litres() == 500.0
    assert [entry["action"] for entry in second.balance.history()] == ["charge", "drain"]


def test_register_enforces_the_volume_window_and_cold_chain(tmp_path: Path) -> None:
    runtime = _open_intake(tmp_path)
    with pytest.raises(RangeError) as too_large:
        runtime.control.receive(5000.0, 6.0, batch_id="B-0501", reason="oversize")
    assert too_large.value.details["maximum"] == 1200.0
    with pytest.raises(RangeError) as too_warm:
        runtime.control.receive(700.0, 40.0, batch_id="B-0501", reason="warm")
    assert too_warm.value.details["field"] == "temperature_c"
    # A refused delivery leaves no receipt and moves no totals.
    assert runtime.intake.total_litres() == 0.0
    assert runtime.intake.receipts() == []


def test_screen_reports_combined_reasons_without_changing_state(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    report = runtime.intake.screen(5000.0, 40.0)
    assert report["accepted"] is False
    assert len(report["reasons"]) == 2
    assert report["window_c"] == [1.0, 10.0]
    assert runtime.intake.total_litres() == 0.0
