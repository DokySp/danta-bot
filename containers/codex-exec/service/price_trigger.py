import html
import logging
import threading
from typing import Any

from .config import Config
from .pipelines.price_monitoring import monitor as price_monitoring
from .telegram_gateway import TelegramGateway, TypingIndicator


def load_price_monitoring_module() -> Any:
    return price_monitoring


class PriceTriggerWatcher:
    def __init__(self, config: Config, gateway: TelegramGateway) -> None:
        self.config = config
        self.gateway = gateway
        self.pipeline = load_price_monitoring_module()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._loop, name="price-trigger-watcher", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            wait_seconds = 60
            try:
                wait_seconds, notifications = self.pipeline.run_price_monitoring_tick(
                    self.config.price_trigger_file,
                    self.config.state_dir,
                    self.config,
                    self._send_touch,
                )
            except Exception:
                logging.exception("price trigger tick failed")
            self.stop_event.wait(wait_seconds)

    def _send_touch(self, notification: Any) -> None:
        trigger = notification.trigger
        quote = notification.quote
        reference = notification.reference
        percent = notification.percent
        direction_label = notification.direction_label
        route = trigger.route
        chat_id = trigger.chat_id
        text = (
            f"<b>{html.escape(trigger.case_title)}</b>\n"
            "<b>가격 조건 터치</b>\n"
            f"아이디: <code>{html.escape(trigger.trigger_id)}</code>\n"
            f"대상: <code>{html.escape(trigger.name)}</code>\n"
            f"방향: {html.escape(direction_label)}\n"
            f"기준값: <code>{reference:,.2f}</code>\n"
            f"터치값: <code>{quote.value:,.2f}</code>\n"
            f"등락률: <code>{percent:+.2f}%</code>\n"
            f"관측시각: <code>{html.escape(quote.observed_at)}</code>"
        )
        if quote.market_status:
            text += f"\n시장상태: <code>{html.escape(quote.market_status)}</code>"
        if not trigger.send_telegram:
            logging.info("price trigger Telegram send disabled id=%s", trigger.trigger_id)
            return
        with TypingIndicator(
            self.gateway,
            chat_id,
            route,
            self.config.telegram_typing_interval_seconds,
        ):
            self.gateway.send_message(text, chat_id, route)
