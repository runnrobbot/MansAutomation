"""Application entrypoint for MansAutomation."""

from __future__ import annotations

import asyncio
import signal
import sys
from typing import NoReturn

import qasync
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication

from mansautomation.core.bootstrap import build_container
from mansautomation.core.container import Container
from mansautomation.gui.main_window import MainWindow
from mansautomation.gui.theme import apply_dark_theme
from mansautomation.services.logging_service import LoggingService


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, app: QApplication) -> None:
    def _shutdown() -> None:
        app.quit()

    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _shutdown)


def main() -> NoReturn:
    """Bootstrap dependency injection container and start the Qt event loop."""

    app = QApplication(sys.argv)
    app.setApplicationName("MansAutomation")
    app.setOrganizationName("MansAutomation")
    app.setQuitOnLastWindowClosed(True)
    app.setWindowIcon(QIcon())
    apply_dark_theme(app)

    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    container: Container = build_container()
    logging_service: LoggingService = container.resolve(LoggingService)
    logger = logging_service.get_logger("bootstrap")
    logger.info("application_starting", version="1.0.0")

    window = MainWindow(container)
    window.show()

    _install_signal_handlers(loop, app)

    async def _startup() -> None:
        await container.start_async_services()
        await window.load_initial_data()
        logger.info("application_ready")

    with loop:
        loop.create_task(_startup())
        try:
            loop.run_forever()
        finally:
            loop.run_until_complete(container.stop_async_services())
            logger.info("application_stopped")

    sys.exit(0)


if __name__ == "__main__":
    main()
