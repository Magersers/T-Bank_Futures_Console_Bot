from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

CONFIG_DIR = Path.home() / ".tbank_futures_bot"
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULT_MIN_NET_PROFIT_PCT = Decimal("0.09")
DEFAULT_STOP_LOSS_PCT = Decimal("0.6")
DEFAULT_COMMISSION_PCT = Decimal("0.05")
DEFAULT_ENTRY_DEVIATION_PCT = Decimal("0.15")
DEFAULT_MAX_ORDERS_PER_SIDE = 3


@dataclass
class TradingSettings:
    long_token: str
    long_account_id: str
    short_token: str
    short_account_id: str
    figi: str
    max_long: int
    max_short: int
    min_net_profit_pct: Decimal
    stop_loss_pct: Decimal
    commission_pct: Decimal
    entry_deviation_pct: Decimal
    max_orders_per_side: int


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


def save_config(data: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return value or (default or "")


def ask_lots(label: str, default: int = 0) -> int:
    while True:
        raw = ask(label, str(default))
        try:
            num = int(raw)
        except ValueError:
            print("Введите целое число от 0 и выше")
            continue
        if num >= 0:
            return num
        print("Ограничение: минимум 0 лотов на сторону.")


def ask_positive_int(label: str, default: int) -> int:
    while True:
        raw = ask(label, str(default))
        try:
            value = int(raw)
        except ValueError:
            print("Введите целое число от 1 и выше")
            continue
        if value >= 1:
            return value
        print("Ограничение: минимум 1.")


def ask_decimal_pct(label: str, default: Decimal) -> Decimal:
    while True:
        raw = ask(label, str(default))
        raw = raw.replace(",", ".")
        try:
            value = Decimal(raw)
        except Exception:
            print("Введите корректное число (например: 0.09)")
            continue
        if value < 0:
            print("Значение должно быть >= 0")
            continue
        return value


def collect_settings() -> TradingSettings:
    cached = load_config()
    print("Введите параметры (Enter = взять из кэша):")

    cached_long_token = cached.get("long_token") or cached.get("token")
    cached_long_account = cached.get("long_account_id") or cached.get("account_id")
    cached_short_token = cached.get("short_token") or cached_long_token
    cached_short_account = cached.get("short_account_id") or cached_long_account

    settings = TradingSettings(
        long_token=ask("Токен LONG (основной аккаунт)", cached_long_token),
        long_account_id=ask("ID портфеля LONG (account_id)", cached_long_account),
        short_token=ask("Токен SHORT (второй аккаунт)", cached_short_token),
        short_account_id=ask("ID портфеля SHORT (account_id)", cached_short_account),
        figi=ask("FIGI фьючерса", cached.get("figi")),
        max_long=ask_lots("Общий объем лотов в LONG (0..N)", cached.get("max_long", 0)),
        max_short=ask_lots("Общий объем лотов в SHORT (0..N)", cached.get("max_short", 0)),
        min_net_profit_pct=ask_decimal_pct(
            "Минимальная чистая прибыль TP, %",
            Decimal(str(cached.get("min_net_profit_pct", DEFAULT_MIN_NET_PROFIT_PCT))),
        ),
        stop_loss_pct=ask_decimal_pct("Стоп-лосс, %", Decimal(str(cached.get("stop_loss_pct", DEFAULT_STOP_LOSS_PCT)))),
        commission_pct=ask_decimal_pct("Комиссия, %", Decimal(str(cached.get("commission_pct", DEFAULT_COMMISSION_PCT)))),
        entry_deviation_pct=ask_decimal_pct(
            "Отклонение цены для L2/L3, %",
            Decimal(str(cached.get("entry_deviation_pct", DEFAULT_ENTRY_DEVIATION_PCT))),
        ),
        max_orders_per_side=ask_positive_int(
            "Максимум сделок на сторону",
            int(cached.get("max_orders_per_side", DEFAULT_MAX_ORDERS_PER_SIDE)),
        ),
    )

    save_config(
        {
            **settings.__dict__,
            "min_net_profit_pct": str(settings.min_net_profit_pct),
            "stop_loss_pct": str(settings.stop_loss_pct),
            "commission_pct": str(settings.commission_pct),
            "entry_deviation_pct": str(settings.entry_deviation_pct),
        }
    )
    return settings
