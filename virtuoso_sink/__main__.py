"""Long-poll consumer entrypoint. Configured entirely via env."""
import logging


from .shutdown import install_stop_handlers
from .sink import VirtuosoSink


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # As PID 1 an unregistered SIGTERM is ignored outright, so without
    # this the pod only ever dies on SIGKILL and stalls every node
    # drain. See .shutdown.
    install_stop_handlers()
    sink = VirtuosoSink.from_env()
    try:
        sink.run_forever()
    finally:
        # Releases the HTTP client to Virtuoso on the way out, on the
        # signal path as well as the error path.
        sink.close()


if __name__ == "__main__":
    main()
