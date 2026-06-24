"""Entry point: serve the web app (production WSGI via waitress)."""

from __future__ import annotations

from . import config
from .logsetup import configure_logging, get_logger
from .web import create_app


def main() -> None:
    configure_logging()
    log = get_logger("app")
    app = create_app()
    log.info(
        "TySpeaker starting on http://%s:%s (samples: %s)",
        config.HOST,
        config.PORT,
        config.SAMPLES_DIR,
    )
    try:
        from waitress import serve

        serve(app, host=config.HOST, port=config.PORT)
    except ImportError:
        app.run(host=config.HOST, port=config.PORT)


if __name__ == "__main__":
    main()
