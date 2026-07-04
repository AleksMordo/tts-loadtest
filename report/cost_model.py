"""Расчёт стоимости канала и секунды синтеза на основе измеренного N_max.

Формулы (раздел 7 ТЗ):
  C_hour              = сумма components из pricing.yaml
  стоимость линии/час = C_hour / N_max
  сек. синтеза (грязная, 100% занятость) = C_hour / (N_max * 3600)
  сек. синтеза (реальная)                = C_hour / (N_max * 3600 * duty_cycle)
  инстансов под X линий = ceil(X / (N_max / headroom))   # headroom=1.3 — запас на пики
  минута разговора = стоимость линии/час / 60
    (эквивалентно 60 * duty_cycle * сек_реальная — полная стоимость линии,
     размазанная по времени разговора)

Никаких цен в коде — только pricing.yaml.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import yaml


@dataclass
class Pricing:
    currency: str
    vat_included: bool
    c_hour: float          # ВМ + диск + IP, за час
    duty_cycle: float
    peak_headroom: float
    hours_per_month: float

    @classmethod
    def load(cls, path: str) -> Pricing:
        with open(path, encoding="utf-8") as fh:
            p = yaml.safe_load(fh)
        c_hour = sum(float(v) for v in p["components"].values())
        duty = float(p["duty_cycle"])
        if not (0.0 < duty <= 1.0):
            raise ValueError(f"duty_cycle должен быть в (0, 1], получено {duty}")
        return cls(
            currency=p.get("currency", "RUB"),
            vat_included=bool(p.get("vat_included", True)),
            c_hour=c_hour,
            duty_cycle=duty,
            peak_headroom=float(p.get("peak_headroom", 1.3)),
            hours_per_month=float(p.get("hours_per_month", 720)),
        )


def line_cost_per_hour(pricing: Pricing, n_max: int) -> float:
    _require_capacity(n_max)
    return pricing.c_hour / n_max


def synth_second_dirty(pricing: Pricing, n_max: int) -> float:
    """Стоимость секунды синтеза при 100% занятости линии."""
    _require_capacity(n_max)
    return pricing.c_hour / (n_max * 3600)


def synth_second_real(pricing: Pricing, n_max: int) -> float:
    """Стоимость секунды синтеза с учётом duty_cycle (бот говорит не всё время)."""
    return synth_second_dirty(pricing, n_max) / pricing.duty_cycle


def instances_for_lines(x_lines: int, n_max: int, headroom: float) -> int:
    """ceil(X / (N_max / headroom)) — запас ёмкости на пики."""
    _require_capacity(n_max)
    return math.ceil(x_lines / (n_max / headroom))


def talk_minute_cost(pricing: Pricing, n_max: int) -> float:
    """₽ за минуту разговора: стоимость занятой линии в течение минуты."""
    return line_cost_per_hour(pricing, n_max) / 60.0


def cost_table(pricing: Pricing, n_max: int, targets: list[int]) -> list[dict]:
    """Таблица для X линий: инстансы, ₽/час, ₽/месяц, ₽/сек синтеза, ₽/мин разговора."""
    rows = []
    for x in targets:
        inst = instances_for_lines(x, n_max, pricing.peak_headroom)
        per_hour = inst * pricing.c_hour
        rows.append({
            "lines": x,
            "instances": inst,
            "cost_per_hour": per_hour,
            "cost_per_month": per_hour * pricing.hours_per_month,
            "synth_second_dirty": synth_second_dirty(pricing, n_max),
            "synth_second_real": synth_second_real(pricing, n_max),
            "talk_minute": talk_minute_cost(pricing, n_max),
        })
    return rows


def _require_capacity(n_max) -> None:
    if not n_max or n_max <= 0:
        raise ValueError(
            "N_max не измерен или равен 0 — расчёт стоимости невозможен "
            "(проверьте результаты сценария nmax)"
        )
