"""Long-poll consumer entrypoint. Configured entirely via env."""
import logging

from gmr_events.consumer import ConsumerConfig

from .sink import VirtuosoSink


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    sink = VirtuosoSink.from_env()
    sink.run_forever()


if __name__ == "__main__":
    main()
