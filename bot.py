#!/usr/bin/env python3
"""Консольный бот для торговли фьючерсами через T-Bank Invest API.

⚠️ Важно: код демонстрационный. Не гарантирует доходность и может приводить к убыткам.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Literal

from t_tech.invest import (
    Client,
    MarketDataRequest,
    OrderBookInstrument,
    OrderDirection,
    OrderType,
    SubscribeOrderBookRequest,
    SubscriptionAction,
)

CONFIG_DIR = Path.home() / ".tbank_futures_bot"
CONFIG_FILE = CONFIG_DIR / "config.json"
DEFAULT_MIN_NET_PROFIT_PCT = Decimal("0.09")
DEFAULT_STOP_LOSS_PCT = Decimal("0.6")
DEFAULT_COMMISSION_PCT = Decimal("0.05")
DEFAULT_ENTRY_DEVIATION_PCT = Decimal("0.15")
DEFAULT_MAX_ORDERS_PER_SIDE = 3

Side = Literal["long", "short"]


@dataclass
class Position:
    level: int
    side: Side
    entry_price: Decimal
    quantity: int
    order_id: str


class FuturesTraderBot:
    def __init__(
        self,
        long_token: str,
        long_account_id: str,
        short_token: str,
        short_account_id: str,
        figi: str,
        max_long: int,
        max_short: int,
        min_net_profit_pct: Decimal,
        stop_loss_pct: Decimal,
        commission_pct: Decimal,
        entry_deviation_pct: Decimal,
        max_orders_per_side: int,
    ) -> None:
        self.long_token = long_token
        self.long_account_id = long_account_id
        self.short_token = short_token
        self.short_account_id = short_account_id
        self.figi = figi
        self.max_long = max_long
        self.max_short = max_short
        self.min_net_profit_pct = min_net_profit_pct
        self.stop_loss_pct = stop_loss_pct
        self.commission_pct = commission_pct
        self.entry_deviation_pct = entry_deviation_pct
        self.max_orders_per_side = max_orders_per_side
        self.long_plan = self._build_lot_plan(max_long)
        self.short_plan = self._build_lot_plan(max_short)
        self.long_positions: dict[int, Position] = {}
        self.short_positions: dict[int, Position] = {}
        self.entry_trigger_state: dict[tuple[Side, int], bool] = {}
        self.long_client: Client | None = None
        self.short_client: Client | None = None

    def _build_lot_plan(self, total_lots: int) -> list[int]:
        """Разбивает общий объем на максимум max_orders_per_side независимых сделок."""
        if total_lots <= 0:
            return []

        chunks = min(self.max_orders_per_side, total_lots)
        base = total_lots // chunks
        remainder = total_lots % chunks
        plan = [base] * chunks
        for i in range(remainder):
            plan[i] += 1
        return plan

    def run(self) -> None:
        print("Запуск бота... Ctrl+C для остановки")
        print(
            f"План LONG: {self.long_plan or [0]} | "
            f"План SHORT: {self.short_plan or [0]} (макс {self.max_orders_per_side} сделок на сторону)"
        )

        with Client(self.long_token) as market_client, Client(self.short_token) as short_client:
            self.long_client = market_client
            self.short_client = short_client
            try:
                for market_data in market_client.market_data_stream.market_data_stream(
                    self._market_data_request_iterator()
                ):
                    if not market_data.orderbook:
                        continue

                    orderbook = market_data.orderbook
                    if not orderbook.bids or not orderbook.asks:
                        continue

                    bid = self._quotation_to_decimal(orderbook.bids[0].price)
                    ask = self._quotation_to_decimal(orderbook.asks[0].price)
                    self.check_exits(bid=bid, ask=ask)

                    self.ensure_entries(bid=bid, ask=ask)
            except KeyboardInterrupt:
                print("\nОстановка по Ctrl+C")

    def _market_data_request_iterator(self):
        yield MarketDataRequest(
            subscribe_order_book_request=SubscribeOrderBookRequest(
                subscription_action=SubscriptionAction.SUBSCRIPTION_ACTION_SUBSCRIBE,
                instruments=[OrderBookInstrument(figi=self.figi, depth=1)],
            )
        )
        while True:
            yield MarketDataRequest()

    @staticmethod
    def _quotation_to_decimal(quotation) -> Decimal:
        return Decimal(quotation.units) + Decimal(quotation.nano) / Decimal(1_000_000_000)

    def ensure_entries(self, bid: Decimal, ask: Decimal) -> None:
        self._open_missing_positions(side="long", entry_price=ask)
        self._open_missing_positions(side="short", entry_price=bid)

    def _open_missing_positions(self, side: Side, entry_price: Decimal) -> None:
        positions = self.long_positions if side == "long" else self.short_positions
        plan = self.long_plan if side == "long" else self.short_plan

        for level in range(1, len(plan) + 1):
            if level in positions:
                continue

            if level == 1:
                self.open_position(side=side, level=level, price=entry_price, quantity=plan[0])
                return

            previous_position = positions.get(level - 1)
            if previous_position is None:
                self.entry_trigger_state[(side, level)] = False
                continue

            trigger_hit = self._is_entry_trigger_hit(
                side=side,
                current_price=entry_price,
                previous_entry=previous_position.entry_price,
            )
            key = (side, level)
            was_trigger_hit = self.entry_trigger_state.get(key, False)
            self.entry_trigger_state[key] = trigger_hit
            if trigger_hit and not was_trigger_hit:
                self.open_position(side=side, level=level, price=entry_price, quantity=plan[level - 1])
            return

    def _is_entry_trigger_hit(self, side: Side, current_price: Decimal, previous_entry: Decimal) -> bool:
        deviation_abs = previous_entry * (self.entry_deviation_pct / Decimal(100))
        if side == "long":
            return current_price <= previous_entry - deviation_abs
        return current_price >= previous_entry + deviation_abs

    def _get_client_and_account(self, side: Side) -> tuple[Client, str]:
        if side == "long":
            if self.long_client is None:
                raise RuntimeError("LONG client не инициализирован")
            return self.long_client, self.long_account_id
        if self.short_client is None:
            raise RuntimeError("SHORT client не инициализирован")
        return self.short_client, self.short_account_id

    def open_position(self, side: Side, level: int, price: Decimal, quantity: int) -> None:
        client, account_id = self._get_client_and_account(side)
        direction = OrderDirection.ORDER_DIRECTION_BUY if side == "long" else OrderDirection.ORDER_DIRECTION_SELL
        oid = str(uuid.uuid4())

        client.orders.post_order(
            instrument_id=self.figi,
            figi=self.figi,
            quantity=quantity,
            direction=direction,
            account_id=account_id,
            order_type=OrderType.ORDER_TYPE_MARKET,
            order_id=oid,
        )

        position = Position(level=level, side=side, entry_price=price, quantity=quantity, order_id=oid)
        if side == "long":
            self.long_positions[level] = position
        else:
            self.short_positions[level] = position

        print(f"[OPEN] {side.upper()} L{level} {quantity} лот(а/ов) @ {price} (id={oid})")

    def check_exits(self, bid: Decimal, ask: Decimal) -> None:
        self._check_side(self.long_positions, exec_price=bid)
        self._check_side(self.short_positions, exec_price=ask)

    def _check_side(self, positions: dict[int, Position], exec_price: Decimal) -> None:
        for level, pos in sorted(list(positions.items())):
            tp_hit = self._is_take_profit(pos, exec_price)
            sl_hit = self._is_stop_loss(pos, exec_price)

            if tp_hit:
                self.close_position(pos, exec_price, reason="TAKE_PROFIT")
            elif sl_hit:
                self.close_position(pos, exec_price, reason="STOP_LOSS")
            else:
                continue
            positions.pop(level, None)

    def _is_take_profit(self, pos: Position, exec_price: Decimal) -> bool:
        net_pnl_pct = self._calculate_net_pnl_pct(pos, exec_price)
        return net_pnl_pct >= self.min_net_profit_pct

    def _is_stop_loss(self, pos: Position, exec_price: Decimal) -> bool:
        gross_pnl_pct = self._calculate_gross_pnl_pct(pos, exec_price)
        return gross_pnl_pct <= -self.stop_loss_pct

    @staticmethod
    def _calculate_gross_pnl_pct(pos: Position, exec_price: Decimal) -> Decimal:
        gross_pnl_pct = ((exec_price / pos.entry_price) - Decimal(1)) * Decimal(100)
        if pos.side == "short":
            gross_pnl_pct = -gross_pnl_pct
        return gross_pnl_pct

    def _calculate_net_pnl_pct(self, pos: Position, exec_price: Decimal) -> Decimal:
        return self._calculate_gross_pnl_pct(pos, exec_price) - self.commission_pct

    def close_position(self, pos: Position, price: Decimal, reason: str) -> None:
        client, account_id = self._get_client_and_account(pos.side)
        direction = (
            OrderDirection.ORDER_DIRECTION_SELL
            if pos.side == "long"
            else OrderDirection.ORDER_DIRECTION_BUY
        )
        oid = str(uuid.uuid4())

        client.orders.post_order(
            instrument_id=self.figi,
            figi=self.figi,
            quantity=pos.quantity,
            direction=direction,
            account_id=account_id,
            order_type=OrderType.ORDER_TYPE_MARKET,
            order_id=oid,
        )

        gross_pnl_pct = self._calculate_gross_pnl_pct(pos, price)
        net_pnl_pct = self._calculate_net_pnl_pct(pos, price)

        pnl_label = "ПРИБЫЛЬ" if net_pnl_pct > 0 else "УБЫТОК"
        print(
            f"[CLOSE][{reason}] {pos.side.upper()} L{pos.level} qty={pos.quantity} @ {price}; "
            f"entry={pos.entry_price}; gross={gross_pnl_pct:.4f}%; "
            f"fee={self.commission_pct:.4f}%; net={net_pnl_pct:.4f}% => {pnl_label}"
        )


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


def collect_settings() -> dict:
    cached = load_config()
    print("Введите параметры (Enter = взять из кэша):")

    cached_long_token = cached.get("long_token") or cached.get("token")
    cached_long_account = cached.get("long_account_id") or cached.get("account_id")
    cached_short_token = cached.get("short_token") or cached_long_token
    cached_short_account = cached.get("short_account_id") or cached_long_account

    long_token = ask("Токен LONG (основной аккаунт)", cached_long_token)
    long_account_id = ask("ID портфеля LONG (account_id)", cached_long_account)
    short_token = ask("Токен SHORT (второй аккаунт)", cached_short_token)
    short_account_id = ask("ID портфеля SHORT (account_id)", cached_short_account)
    figi = ask("FIGI фьючерса", cached.get("figi"))
    max_long = ask_lots("Общий объем лотов в LONG (0..N)", cached.get("max_long", 0))
    max_short = ask_lots("Общий объем лотов в SHORT (0..N)", cached.get("max_short", 0))
    min_net_profit_pct = ask_decimal_pct(
        "Минимальная чистая прибыль TP, %", Decimal(str(cached.get("min_net_profit_pct", DEFAULT_MIN_NET_PROFIT_PCT)))
    )
    stop_loss_pct = ask_decimal_pct("Стоп-лосс, %", Decimal(str(cached.get("stop_loss_pct", DEFAULT_STOP_LOSS_PCT))))
    commission_pct = ask_decimal_pct("Комиссия, %", Decimal(str(cached.get("commission_pct", DEFAULT_COMMISSION_PCT))))
    entry_deviation_pct = ask_decimal_pct(
        "Отклонение цены для L2/L3, %", Decimal(str(cached.get("entry_deviation_pct", DEFAULT_ENTRY_DEVIATION_PCT)))
    )
    max_orders_per_side = ask_positive_int(
        "Максимум сделок на сторону", int(cached.get("max_orders_per_side", DEFAULT_MAX_ORDERS_PER_SIDE))
    )

    settings = {
        "long_token": long_token,
        "long_account_id": long_account_id,
        "short_token": short_token,
        "short_account_id": short_account_id,
        "figi": figi,
        "max_long": max_long,
        "max_short": max_short,
        "min_net_profit_pct": min_net_profit_pct,
        "stop_loss_pct": stop_loss_pct,
        "commission_pct": commission_pct,
        "entry_deviation_pct": entry_deviation_pct,
        "max_orders_per_side": max_orders_per_side,
    }
    save_config(
        {
            **settings,
            "min_net_profit_pct": str(min_net_profit_pct),
            "stop_loss_pct": str(stop_loss_pct),
            "commission_pct": str(commission_pct),
            "entry_deviation_pct": str(entry_deviation_pct),
        }
    )
    return settings


def main() -> None:
    settings = collect_settings()
    if settings["max_long"] == 0 and settings["max_short"] == 0:
        print("Оба направления отключены (0/0). Нечего торговать.")
        return

    bot = FuturesTraderBot(**settings)
    bot.run()


if __name__ == "__main__":
    main()
