"""Launch the hardware-free slmcore Qt reference host."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
import sys

from qtpy import QtWidgets

from .host import DemoHost
from .theme import apply_demo_theme
from .window import DemoMainWindow


def _parse_args(argv: list[str] | None=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the hardware-free slmcore Qt demo host.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path.home() / ".slmcore-demo",
        help="Demo data root used for the slmcore workspace.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable DEBUG logging for the demo and slmcore.",
    )
    return parser.parse_args(argv)


def _configure_logging(debug: bool) -> None:
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("slmcore").setLevel(level)
    logging.getLogger("examples.qt_demo").setLevel(level)


def main(argv: list[str] | None=None) -> int:
    args = _parse_args(argv)
    _configure_logging(args.debug)

    app = QtWidgets.QApplication.instance()
    owns_app = app is None
    if app is None:
        app = QtWidgets.QApplication(sys.argv[:1])

    apply_demo_theme(app)

    window = DemoMainWindow()
    host = DemoHost(window,data_dir=args.data_dir)
    try:
        host.initialize()
    except Exception as error:
        logging.getLogger(__name__).exception("Demo initialization failed")
        window.show_error("slmcore demo initialization failed",error)
        host.dispose()
        return 1

    app.aboutToQuit.connect(host.dispose)
    window.show()
    if not owns_app:
        return 0
    exec_method = getattr(app,"exec",None) or app.exec_
    return int(exec_method())


if __name__ == "__main__":
    raise SystemExit(main())
