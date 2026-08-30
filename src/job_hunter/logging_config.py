import logging


def configure_logging(*, verbose: bool = False, debug: bool = False) -> None:
    level = logging.DEBUG if (verbose or debug) else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
