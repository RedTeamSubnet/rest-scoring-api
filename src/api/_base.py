import threading
import time
import traceback
from abc import ABC, abstractmethod

import logging

from .config import ScoringApiMainConfig

logger = logging.getLogger(__name__)


class BaseScoringApi(ABC):
    def __init__(self):
        self.scoring_api_config = ScoringApiMainConfig()
        self.setup_logging()
        self.forward_thread: threading.Thread | None = None

    def setup_logging(self) -> None:
        log_level = self.scoring_api_config.LOGGING_LEVEL.upper()
        level = logging.DEBUG if log_level == "TRACE" else getattr(logging, log_level)
        logging.basicConfig(
            level=level,
            format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        )
        logging.getLogger().setLevel(level)
        logger.info(
            "Starting scoring API: core=%s port=%s poll_interval=%ss",
            self.scoring_api_config.CORE_API_URL,
            self.scoring_api_config.PORT,
            self.scoring_api_config.POLL_INTERVAL,
        )

    def run(self) -> None:
        """Run scoring passes at the configured polling interval."""
        logger.info("Starting scoring API loop.")
        while True:
            if self.forward_thread is None or not self.forward_thread.is_alive():
                self.forward_thread = threading.Thread(
                    target=self._run_forward,
                    daemon=True,
                    name="scoring_api_forward_thread",
                )
                self.forward_thread.start()
                logger.info("Started new forward thread")
            time.sleep(self.scoring_api_config.POLL_INTERVAL)

    def _run_forward(self) -> None:
        """Run a single forward pass in a separate thread."""
        try:
            start_time = time.time()
            self.forward()
            elapsed = time.time() - start_time
            logger.info("Forward completed in %.2f seconds", elapsed)
        except Exception:
            logger.error(f"Forward error: {traceback.format_exc()}")

    @abstractmethod
    def forward(self) -> None:
        pass
