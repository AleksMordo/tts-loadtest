"""Юнит-тесты модели стоимости (формулы раздела 7 ТЗ)."""

import math

import pytest

from report import cost_model

PRICING_YAML = """\
currency: RUB
vat_included: true
components:
  gpu_vm_per_hour: 900.0
  disk_per_hour: 10.0
  public_ip_per_hour: 5.0
duty_cycle: 0.4
peak_headroom: 1.3
hours_per_month: 720
"""


@pytest.fixture
def pricing(tmp_path):
    p = tmp_path / "pricing.yaml"
    p.write_text(PRICING_YAML, encoding="utf-8")
    return cost_model.Pricing.load(str(p))


def test_c_hour_is_sum_of_components(pricing):
    assert pricing.c_hour == 915.0


def test_line_cost_per_hour(pricing):
    assert cost_model.line_cost_per_hour(pricing, 30) == pytest.approx(915.0 / 30)


def test_synth_second_dirty_and_real(pricing):
    dirty = cost_model.synth_second_dirty(pricing, 30)
    assert dirty == pytest.approx(915.0 / (30 * 3600))
    real = cost_model.synth_second_real(pricing, 30)
    assert real == pytest.approx(dirty / 0.4)


def test_instances_with_headroom(pricing):
    # ceil(X / (N_max / 1.3))
    assert cost_model.instances_for_lines(10, 30, 1.3) == math.ceil(10 / (30 / 1.3))  # 1
    assert cost_model.instances_for_lines(30, 30, 1.3) == 2   # 30/(30/1.3)=1.3 -> 2
    assert cost_model.instances_for_lines(100, 30, 1.3) == 5  # 100/23.07 -> 5


def test_talk_minute_equals_line_hour_over_60(pricing):
    assert cost_model.talk_minute_cost(pricing, 30) == pytest.approx(915.0 / 30 / 60)
    # эквивалентность: 60 * duty * секунда_реальная
    assert cost_model.talk_minute_cost(pricing, 30) == pytest.approx(
        60 * 0.4 * cost_model.synth_second_real(pricing, 30)
    )


def test_cost_table(pricing):
    rows = cost_model.cost_table(pricing, 30, [10, 30, 100])
    assert [r["lines"] for r in rows] == [10, 30, 100]
    assert rows[2]["instances"] == 5
    assert rows[2]["cost_per_hour"] == pytest.approx(5 * 915.0)
    assert rows[2]["cost_per_month"] == pytest.approx(5 * 915.0 * 720)


def test_zero_nmax_rejected(pricing):
    with pytest.raises(ValueError):
        cost_model.synth_second_dirty(pricing, 0)
    with pytest.raises(ValueError):
        cost_model.line_cost_per_hour(pricing, None)


def test_bad_duty_cycle_rejected(tmp_path):
    p = tmp_path / "pricing.yaml"
    p.write_text(PRICING_YAML.replace("duty_cycle: 0.4", "duty_cycle: 0"), encoding="utf-8")
    with pytest.raises(ValueError):
        cost_model.Pricing.load(str(p))
