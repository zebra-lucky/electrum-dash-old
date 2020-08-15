#!/usr/bin/env python3

import os
import asyncio

from electrum_dash.simple_config import SimpleConfig
from electrum_dash import constants
from electrum_dash.daemon import Daemon
from electrum_dash.storage import WalletStorage
from electrum_dash.wallet import Wallet, create_new_wallet
from electrum_dash.wallet_db import WalletDB
from electrum_dash.commands import Commands
from electrum_dash.util import create_and_start_event_loop, log_exceptions


loop, stopping_fut, loop_thread = create_and_start_event_loop()

config = SimpleConfig({"testnet": True})  # to use ~/.electrum-dash/testnet as datadir
constants.set_testnet()  # to set testnet magic bytes
daemon = Daemon(config, listen_jsonrpc=False)
network = daemon.network
assert network.asyncio_loop.is_running()

# get wallet on disk
wallet_dir = os.path.dirname(config.get_wallet_path())
wallet_path = os.path.join(wallet_dir, "test_wallet")
if not os.path.exists(wallet_path):
    create_new_wallet(path=wallet_path, config=config)

# open wallet
wallet = daemon.load_wallet(wallet_path, password=None, manual_upgrades=False)
wallet.start_network(network)

# you can use ~CLI commands by accessing command_runner
command_runner = Commands(config=config, daemon=daemon, network=network)
print("balance", network.run_from_another_thread(command_runner.getbalance(wallet=wallet)))
print("addr",    network.run_from_another_thread(command_runner.getunusedaddress(wallet=wallet)))
print("gettx",   network.run_from_another_thread(
    command_runner.gettransaction("d8ee577f6b864071c6ccbac1e30d0d19edd6fa9a171be02b85a73fd533f2734d")))


# but you might as well interact with the underlying methods directly
print("balance", wallet.get_balance())
print("addr",    wallet.get_unused_address())
print("gettx",   network.run_from_another_thread(network.get_transaction("d8ee577f6b864071c6ccbac1e30d0d19edd6fa9a171be02b85a73fd533f2734d")))

stopping_fut.set_result(1)  # to stop event loop
