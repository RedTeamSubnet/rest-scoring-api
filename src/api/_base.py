import os
import threading
import time
import traceback
from abc import ABC, abstractmethod

import bittensor as bt
from substrateinterface import SubstrateInterface
from redteam_core.config import MainConfig
from .config import ScoringApiMainConfig


class BaseScoringApi(ABC):
    def __init__(self):
        self.config: MainConfig = MainConfig()
        self.scoring_api_config: ScoringApiMainConfig = ScoringApiMainConfig()

        self.setup_logging()
        self.setup_bittensor_objects()
        self.last_update = 0
        self.current_block = 0
        self.node = SubstrateInterface(url=self.config.BITTENSOR.SUBTENSOR_NETWORK)
        self.is_running = False
        self.forward_thread: threading.Thread = None

    def setup_logging(self):
        bt.logging.enable_default()
        bt.logging.enable_info()
        if self.config.BITTENSOR.LOGGING_LEVEL == "DEBUG":
            bt.logging.enable_debug()
        elif self.config.BITTENSOR.LOGGING_LEVEL == "TRACE":
            bt.logging.enable_trace()
        bt.logging.info(
            f"Running validator for subnet: {self.config.BITTENSOR.SUBNET_NETUID} on network: {self.config.BITTENSOR.SUBTENSOR_NETWORK} with config:"
        )
        bt.logging.info(self.config.model_dump_json())

    def setup_bittensor_objects(self):
        bt.logging.info("Setting up Bittensor objects.")

        bt_config = self._create_bittensor_config()

        self.wallet = bt.wallet(config=bt_config)
        bt.logging.info(f"Wallet: {self.wallet}")

        self.subtensor = bt.subtensor(config=bt_config)
        bt.logging.info(f"Subtensor: {self.subtensor}")

        self.dendrite = bt.dendrite(wallet=self.wallet)
        bt.logging.info(f"Dendrite: {self.dendrite}")

        self.metagraph = self.subtensor.metagraph(self.config.BITTENSOR.SUBNET_NETUID)
        bt.logging.info(f"Metagraph: {self.metagraph}")

        # if self.wallet.hotkey.ss58_address not in self.metagraph.hotkeys:
        #     bt.logging.error(
        #         f"\nYour validator: {self.wallet} is not registered to chain connection: {self.subtensor} \nRun 'btcli register' and try again."
        #     )
        #     exit()
        # else:
        #     self.uid = self.metagraph.hotkeys.index(self.wallet.hotkey.ss58_address)

        self.hotkey = self.wallet.hotkey.ss58_address
        self.uid = self.scoring_api_config.UID
        bt.logging.info(f"Running validator on uid: {self.uid}")

    def run(self):
        bt.logging.info("Starting scoring API loop.")
        while True:
            # Check if we need to start a new forward thread
            if self.forward_thread is None or not self.forward_thread.is_alive():
                # Start new forward thread
                self.forward_thread = threading.Thread(
                    target=self._run_forward,
                    daemon=True,
                    name="scoring_api_forward_thread",
                )
                self.forward_thread.start()
                bt.logging.info("Started new forward thread")
            try:
                self.resync_metagraph()
                bt.logging.success("Resync metagraph completed")
            except Exception:
                bt.logging.error(f"Resync metagraph error: {traceback.format_exc()}")
            except KeyboardInterrupt:
                bt.logging.success("Keyboard interrupt detected. Exiting validator.")
                exit()

            # Sleep until next weight update
            time.sleep(self.config.EPOCH_LENGTH)

    def synthetic_loop_in_background_thread(self):
        """
        Starts the validator's operations in a background thread upon entering the context.
        This method facilitates the use of the validator in a 'with' statement.
        """
        if not self.is_running:
            bt.logging.debug("Starting validator in background thread.")
            self.should_exit = False
            self.thread = threading.Thread(target=self.run, daemon=True)
            self.thread.start()
            self.is_running = True
            bt.logging.debug("Started")

    def resync_metagraph(self):
        self.metagraph.sync()

    def _run_forward(self):
        """Run a single forward pass in a separate thread."""
        try:
            start_time = time.time()
            self.forward()
            elapsed = time.time() - start_time
            bt.logging.success(f"Forward completed in {elapsed:.2f} seconds")
        except Exception:
            bt.logging.error(f"Forward error: {traceback.format_exc()}")

    def _create_bittensor_config(self) -> bt.Config:
        """
        Create a Bittensor Config object from MainConfig.

        Maps the hierarchical MainConfig structure to Bittensor's expected Config format.

        Returns:
            bt.Config: Bittensor configuration object
        """
        bt_config = bt.Config()
        # Set wallet configuration
        if bt_config.wallet is None:
            bt_config.wallet = bt.Config()
        bt_config.wallet.path = os.getenv(
            "RT_BTCLI_WALLET_DIR", self.scoring_api_config.WALLET_DIR
        )
        bt_config.wallet.name = self.scoring_api_config.WALLET_NAME
        bt_config.wallet.hotkey = self.scoring_api_config.HOTKEY_NAME

        if bt_config.subtensor is None:
            bt_config.subtensor = bt.Config()
        bt_config.subtensor.network = self.config.BITTENSOR.SUBTENSOR_NETWORK

        # Set netuid (subnet configuration)
        bt_config.netuid = self.config.BITTENSOR.SUBNET_NETUID

        return bt_config

    def __enter__(self):
        self.synthetic_loop_in_background_thread()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        """
        Stops the validator's background operations upon exiting the context.
        This method facilitates the use of the validator in a 'with' statement.

        Args:
            exc_type: The type of the exception that caused the context to be exited.
                      None if the context was exited without an exception.
            exc_value: The instance of the exception that caused the context to be exited.
                       None if the context was exited without an exception.
            traceback: A traceback object encoding the stack trace.
                       None if the context was exited without an exception.
        """
        if self.is_running:
            bt.logging.debug("Stopping validator in background thread.")
            self.should_exit = True
            # Clean up when exiting
            self.thread.join(5)
            if self.forward_thread and self.forward_thread.is_alive():
                bt.logging.info("Waiting for forward thread to complete...")
                self.forward_thread.join(timeout=5)  # Give thread 5 seconds to finish
            self.is_running = False
            bt.logging.debug("Stopped")

    @abstractmethod
    def forward(self):
        pass
