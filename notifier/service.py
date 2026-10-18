import logging
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from .config import Settings
from .delivery import Delivery, DeliveryError
from .state import State
from .stores import Stores

LOG = logging.getLogger("notifier")


def cycle(settings, state, stores, delivery, stop):
    with ThreadPoolExecutor(max_workers=2) as sources:
        futures = {
            sources.submit(getattr(stores, name)): name for name in ("epic", "steam")
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                offers = future.result()
            except Exception as exc:
                LOG.warning(
                    "%s check failed (%s); retry next cycle", name, type(exc).__name__
                )
                continue
            if stop.is_set():
                continue
            state.observe(name, offers, settings.notify_existing)
            LOG.info("%s: %d confirmed active offers", name, len(offers))
            for offer in offers:
                if stop.is_set():
                    break
                if not offer.active():
                    continue
                channels = [
                    c
                    for c in ("telegram", "discord")
                    if state.pending(offer, c) and state.ready(c, time.time())
                ]
                with ThreadPoolExecutor(max_workers=2) as senders:
                    sends = {
                        senders.submit(getattr(delivery, c), offer): c for c in channels
                    }
                    for send in as_completed(sends):
                        channel = sends[send]
                        try:
                            send.result()
                        except Exception as exc:
                            delay = getattr(exc, "retry_after", 0)
                            if delay:
                                state.defer(channel, time.time() + delay)
                            LOG.warning(
                                "%s delivery failed (%s); retry after fresh store confirmation",
                                channel,
                                str(exc)
                                if isinstance(exc, DeliveryError)
                                else type(exc).__name__,
                            )
                        else:
                            state.delivered(offer, channel)
                            LOG.info("%s delivery confirmed (%s)", channel, name)


def main():
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    state = None
    try:
        try:
            settings = Settings.from_env()
        except ValueError as exc:
            LOG.error("Configuration: %s", exc)
            return 1
        state = State(settings.state_dir)
        stores, delivery = Stores(settings.country, stop), Delivery(settings)
        LOG.info("Service started; check interval %d minutes", settings.interval)
        while not stop.is_set():
            cycle(settings, state, stores, delivery, stop)
            stop.wait(settings.interval * 60)
    except Exception as exc:
        LOG.error(
            "Service stopped (%s); check state directory and configuration",
            type(exc).__name__,
        )
        return 1
    finally:
        if state is not None:
            state.close()
    LOG.info("Service stopped cleanly")
    return 0
