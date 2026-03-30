#!/usr/bin/env python3
"""Консольный бот для торговли фьючерсами через T-Bank Invest API.

⚠️ Важно: код демонстрационный. Не гарантирует доходность и может приводить к убыткам.
"""

from __future__ import annotations

import json
import time
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
MIN_NET_PROFIT_PCT = Decimal("0.09")  # минимальная чистая прибыль, %
STOP_LOSS = Decimal("0.002")  # 0.2%
COMMISSION_PCT = Decimal("0.05")  # 0.05% комиссии на сделку (вычитается в отчете)
COOLDOWN_SECONDS = 120
MAX_ORDERS_PER_SIDE = 3

Side = Literal["long", "short"]


@dataclass
class Position:
    side: Side
    entry_price: Decimal
    quantity: int
    order_id: str


class FuturesTraderBot:
    def __init__(self, token: str, account_id: str, figi: str, max_long: int, max_short: int) -> None:
        self.token = token
        self.account_id = account_id
        self.figi = figi
        self.max_long = max_long
        self.max_short = max_short
        self.long_plan = self._build_lot_plan(max_long)
        self.short_plan = self._build_lot_plan(max_short)
        self.long_positions: list[Position] = []
        self.short_positions: list[Position] = []
        self.cooldown_until: float = 0

    @staticmethod
    def _build_lot_plan(total_lots: int) -> list[int]:
        """Разбивает общий объем на максимум 3 независимые сделки."""
        if total_lots <= 0:
            return []

        chunks = min(MAX_ORDERS_PER_SIDE, total_lots)
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
            f"План SHORT: {self.short_plan or [0]} (макс {MAX_ORDERS_PER_SIDE} сделок на сторону)"
        )

        with Client(self.token) as client:
            try:
                for market_data in client.market_data_stream.market_data_stream(self._market_data_request_iterator()):
                    if not market_data.orderbook:
                        continue

                    orderbook = market_data.orderbook
                    if not orderbook.bids or not orderbook.asks:
                        continue

                    bid = self._quotation_to_decimal(orderbook.bids[0].price)
                    ask = self._quotation_to_decimal(orderbook.asks[0].price)
                    self.check_exits(client, bid=bid, ask=ask)

                    now = time.time()
                    if now >= self.cooldown_until:
                        self.ensure_entries(client, bid=bid, ask=ask)
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

    def ensure_entries(self, client: Client, bid: Decimal, ask: Decimal) -> None:
        self._open_missing_positions(client, side="long", entry_price=ask)
        self._open_missing_positions(client, side="short", entry_price=bid)

    def _open_missing_positions(self, client: Client, side: Side, entry_price: Decimal) -> None:
        positions = self.long_positions if side == "long" else self.short_positions
        plan = self.long_plan if side == "long" else self.short_plan

        while len(positions) < len(plan):
            quantity = plan[len(positions)]
            self.open_position(client, side=side, price=entry_price, quantity=quantity)
            positions = self.long_positions if side == "long" else self.short_positions

    def open_position(self, client: Client, side: Side, price: Decimal, quantity: int) -> None:
        direction = OrderDirection.ORDER_DIRECTION_BUY if side == "long" else OrderDirection.ORDER_DIRECTION_SELL
        oid = str(uuid.uuid4())

        client.orders.post_order(
            instrument_id=self.figi,
            figi=self.figi,
            quantity=quantity,
            direction=direction,
            account_id=self.account_id,
            order_type=OrderType.ORDER_TYPE_MARKET,
            order_id=oid,
        )

        position = Position(side=side, entry_price=price, quantity=quantity, order_id=oid)
        if side == "long":
            self.long_positions.append(position)
        else:
            self.short_positions.append(position)

        print(f"[OPEN] {side.upper()} {quantity} лот(а/ов) @ {price} (id={oid})")

    def check_exits(self, client: Client, bid: Decimal, ask: Decimal) -> None:
        self.long_positions = self._check_side(client, self.long_positions, exec_price=bid)
        self.short_positions = self._check_side(client, self.short_positions, exec_price=ask)

    def _check_side(self, client: Client, positions: list[Position], exec_price: Decimal) -> list[Position]:
        alive: list[Position] = []

        for pos in positions:
            tp_hit = self._is_take_profit(pos, exec_price)
            sl_hit = self._is_stop_loss(pos, exec_price)

            if tp_hit:
                self.close_position(client, pos, exec_price, reason="TAKE_PROFIT")
            elif sl_hit:
                self.close_position(client, pos, exec_price, reason="STOP_LOSS")
                self.cooldown_until = time.time() + COOLDOWN_SECONDS
            else:
                alive.append(pos)

        return alive

    def _is_take_profit(self, pos: Position, exec_price: Decimal) -> bool:
        net_pnl_pct = self._calculate_net_pnl_pct(pos, exec_price)
        return net_pnl_pct >= MIN_NET_PROFIT_PCT

    def _is_stop_loss(self, pos: Position, exec_price: Decimal) -> bool:
        gross_pnl_pct = self._calculate_gross_pnl_pct(pos, exec_price)
        return gross_pnl_pct <= -(STOP_LOSS * Decimal(100))

    @staticmethod
    def _calculate_gross_pnl_pct(pos: Position, exec_price: Decimal) -> Decimal:
        gross_pnl_pct = ((exec_price / pos.entry_price) - Decimal(1)) * Decimal(100)
        if pos.side == "short":
            gross_pnl_pct = -gross_pnl_pct
        return gross_pnl_pct

    def _calculate_net_pnl_pct(self, pos: Position, exec_price: Decimal) -> Decimal:
        return self._calculate_gross_pnl_pct(pos, exec_price) - COMMISSION_PCT

    def close_position(self, client: Client, pos: Position, price: Decimal, reason: str) -> None:
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
            account_id=self.account_id,
            order_type=OrderType.ORDER_TYPE_MARKET,
            order_id=oid,
        )

        gross_pnl_pct = self._calculate_gross_pnl_pct(pos, price)
        net_pnl_pct = self._calculate_net_pnl_pct(pos, price)

        pnl_label = "ПРИБЫЛЬ" if net_pnl_pct > 0 else "УБЫТОК"
        print(
            f"[CLOSE][{reason}] {pos.side.upper()} qty={pos.quantity} @ {price}; "
            f"entry={pos.entry_price}; gross={gross_pnl_pct:.4f}%; "
            f"fee={COMMISSION_PCT:.4f}%; net={net_pnl_pct:.4f}% => {pnl_label}"
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


def collect_settings() -> dict:
    cached = load_config()
    print("Введите параметры (Enter = взять из кэша):")

    token = ask("Токен T-Bank Invest API", cached.get("token"))
    account_id = ask("ID портфеля (account_id)", cached.get("account_id"))
    figi = ask("FIGI фьючерса", cached.get("figi"))
    max_long = ask_lots("Общий объем лотов в LONG (0..N)", cached.get("max_long", 0))
    max_short = ask_lots("Общий объем лотов в SHORT (0..N)", cached.get("max_short", 0))

    settings = {
        "token": token,
        "account_id": account_id,
        "figi": figi,
        "max_long": max_long,
        "max_short": max_short,
    }
    save_config(settings)
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
