import os
import gzip
import unittest
import tempfile
import shutil
from unittest import mock
from decimal import Decimal

from electrum_dash.commands import Commands, eval_bool
from electrum_dash import storage
from electrum_dash.simple_config import SimpleConfig
from electrum_dash.storage import WalletStorage
from electrum_dash.wallet import restore_wallet_from_text, Wallet

from . import TestCaseForTestnet


class TestCommands(unittest.TestCase):

    def test_setconfig_non_auth_number(self):
        self.assertEqual(7777, Commands._setconfig_normalize_value('rpcport', "7777"))
        self.assertEqual(7777, Commands._setconfig_normalize_value('rpcport', '7777'))
        self.assertAlmostEqual(Decimal(2.3), Commands._setconfig_normalize_value('somekey', '2.3'))

    def test_setconfig_non_auth_number_as_string(self):
        self.assertEqual("7777", Commands._setconfig_normalize_value('somekey', "'7777'"))

    def test_setconfig_non_auth_boolean(self):
        self.assertEqual(True, Commands._setconfig_normalize_value('show_console_tab', "true"))
        self.assertEqual(True, Commands._setconfig_normalize_value('show_console_tab', "True"))

    def test_setconfig_non_auth_list(self):
        self.assertEqual(['file:///var/www/', 'https://electrum.org'],
            Commands._setconfig_normalize_value('url_rewrite', "['file:///var/www/','https://electrum.org']"))
        self.assertEqual(['file:///var/www/', 'https://electrum.org'],
            Commands._setconfig_normalize_value('url_rewrite', '["file:///var/www/","https://electrum.org"]'))

    def test_setconfig_auth(self):
        self.assertEqual("7777", Commands._setconfig_normalize_value('rpcuser', "7777"))
        self.assertEqual("7777", Commands._setconfig_normalize_value('rpcuser', '7777'))
        self.assertEqual("7777", Commands._setconfig_normalize_value('rpcpassword', '7777'))
        self.assertEqual("2asd", Commands._setconfig_normalize_value('rpcpassword', '2asd'))
        self.assertEqual("['file:///var/www/','https://electrum.org']",
            Commands._setconfig_normalize_value('rpcpassword', "['file:///var/www/','https://electrum.org']"))

    def test_eval_bool(self):
        self.assertFalse(eval_bool("False"))
        self.assertFalse(eval_bool("false"))
        self.assertFalse(eval_bool("0"))
        self.assertTrue(eval_bool("True"))
        self.assertTrue(eval_bool("true"))
        self.assertTrue(eval_bool("1"))

    def test_convert_xkey(self):
        cmds = Commands(config=None, wallet=None, network=None)
        xpubs = {
            ("xpub6CCWFbvCbqF92kGwm9nV7t7RvVoQUKaq5USMdyVP6jvv1NgN52KAX6NNYCeE8Ca7JQC4K5tZcnQrubQcjJ6iixfPs4pwAQJAQgTt6hBjg11", "standard"),
        }
        for xkey1, xtype1 in xpubs:
            for xkey2, xtype2 in xpubs:
                self.assertEqual(xkey2, cmds.convert_xkey(xkey1, xtype2))

        xprvs = {
            ("xprv9yD9r6PJmTgqpGCUf8FUkkAhNTxv4rryiFWkqb5mYQPw8aMDXUzuyJ3tgv5vUqYkdK1E6Q5jKxPss4HkMBYV4q8AfG8t7rxgyS4xQX4ndAm", "standard"),
        }
        for xkey1, xtype1 in xprvs:
            for xkey2, xtype2 in xprvs:
                self.assertEqual(xkey2, cmds.convert_xkey(xkey1, xtype2))

    @mock.patch.object(storage.WalletStorage, '_write')
    def test_encrypt_decrypt(self, mock_write):
        wallet = restore_wallet_from_text('p2pkh:XJvTzLoBy3jPMZFSTzK6KqTiNR3n5xbreSScEy7u9C8fEf1GZG3X',
                                          path='if_this_exists_mocking_failed_648151893')['wallet']
        cmds = Commands(config=None, wallet=wallet, network=None)
        cleartext = "asdasd this is the message"
        pubkey = "021f110909ded653828a254515b58498a6bafc96799fb0851554463ed44ca7d9da"
        ciphertext = cmds.encrypt(pubkey, cleartext)
        self.assertEqual(cleartext, cmds.decrypt(pubkey, ciphertext))

    @mock.patch.object(storage.WalletStorage, '_write')
    def test_export_private_key_imported(self, mock_write):
        wallet = restore_wallet_from_text('p2pkh:XGx8LpkmLRv9RiMvpYx965BCaQKQbeMVVqgAh7B5SQVdosQiKJ4i p2pkh:XEn9o6oayjsRmoEQwDbvkrWVvjRNqPj3xNskJJPAKraJTrWuutwd',
                                          path='if_this_exists_mocking_failed_648151893')['wallet']
        cmds = Commands(config=None, wallet=wallet, network=None)
        # single address tests
        with self.assertRaises(Exception):
            cmds.getprivatekeys("asdasd")  # invalid addr, though might raise "not in wallet"
        with self.assertRaises(Exception):
            cmds.getprivatekeys("XdDHzW6aTeuQsraNXeEsPy5gAv1nUz7Y7Q")  # not in wallet
        self.assertEqual("p2pkh:XEn9o6oayjsRmoEQwDbvkrWVvjRNqPj3xNskJJPAKraJTrWuutwd",
                         cmds.getprivatekeys("Xci5KnMVkHrqBQk9cU4jwmzJfgaTPopHbz"))
        # list of addresses tests
        with self.assertRaises(Exception):
            cmds.getprivatekeys(['XmQ3Tn67Fgs7bwNXthtiEnBFh7ZeDG3aw2', 'asd'])
        self.assertEqual(['p2pkh:XGx8LpkmLRv9RiMvpYx965BCaQKQbeMVVqgAh7B5SQVdosQiKJ4i', 'p2pkh:XEn9o6oayjsRmoEQwDbvkrWVvjRNqPj3xNskJJPAKraJTrWuutwd'],
                         cmds.getprivatekeys(['XmQ3Tn67Fgs7bwNXthtiEnBFh7ZeDG3aw2', 'Xci5KnMVkHrqBQk9cU4jwmzJfgaTPopHbz']))

    @mock.patch.object(storage.WalletStorage, '_write')
    def test_export_private_key_deterministic(self, mock_write):
        wallet = restore_wallet_from_text('hint shock chair puzzle shock traffic drastic note dinosaur mention suggest sweet',
                                          gap_limit=2,
                                          path='if_this_exists_mocking_failed_648151893')['wallet']
        cmds = Commands(config=None, wallet=wallet, network=None)
        # single address tests
        with self.assertRaises(Exception):
            cmds.getprivatekeys("asdasd")  # invalid addr, though might raise "not in wallet"
        with self.assertRaises(Exception):
            cmds.getprivatekeys("XdDHzW6aTeuQsraNXeEsPy5gAv1nUz7Y7Q")  # not in wallet
        self.assertEqual("p2pkh:XE5VEmWKQRK5N7kQMfw6KqoRp3ExKWgaeCKsxsmDFBxJJBgdQdTH",
                         cmds.getprivatekeys("XvmHzyQe8QWbvv17wc1PPMyJgaomknSp7W"))
        # list of addresses tests
        with self.assertRaises(Exception):
            cmds.getprivatekeys(['XvmHzyQe8QWbvv17wc1PPMyJgaomknSp7W', 'asd'])
        self.assertEqual(['p2pkh:XE5VEmWKQRK5N7kQMfw6KqoRp3ExKWgaeCKsxsmDFBxJJBgdQdTH', 'p2pkh:XGtpLmVGmaRnfvRvd4qxSeE7PqJoi9FUfkgPKD24PeoJsZCh1EXg'],
                         cmds.getprivatekeys(['XvmHzyQe8QWbvv17wc1PPMyJgaomknSp7W', 'XoEUKPPiPETff1S4oQmo4HGR1rYrRAX6uT']))


class TestCommandsTestnet(TestCaseForTestnet):

    def test_convert_xkey(self):
        cmds = Commands(config=None, wallet=None, network=None)
        xpubs = {
            ("tpubD8p5qNfjczgTGbh9qgNxsbFgyhv8GgfVkmp3L88qtRm5ibUYiDVCrn6WYfnGey5XVVw6Bc5QNQUZW5B4jFQsHjmaenvkFUgWtKtgj5AdPm9", "standard"),
        }
        for xkey1, xtype1 in xpubs:
            for xkey2, xtype2 in xpubs:
                self.assertEqual(xkey2, cmds.convert_xkey(xkey1, xtype2))

        xprvs = {
            ("tprv8c83gxdVUcznP8fMx2iNUBbaQgQC7MUbBUDG3c6YU9xgt7Dn5pfcgHUeNZTAvuYmNgVHjyTzYzGWwJr7GvKCm2FkPaaJipyipbfJeB3tdPW", "standard"),
        }
        for xkey1, xtype1 in xprvs:
            for xkey2, xtype2 in xprvs:
                self.assertEqual(xkey2, cmds.convert_xkey(xkey1, xtype2))


class TestTxCommandsTestnet(TestCaseForTestnet):

    def setUp(self):
        super(TestTxCommandsTestnet, self).setUp()
        self.user_dir = tempfile.mkdtemp()
        self.wallet_path = os.path.join(self.user_dir, 'wallet_ps1')
        tests_path = os.path.dirname(os.path.abspath(__file__))
        test_data_file = os.path.join(tests_path, 'data', 'wallet_ps1.gz')
        shutil.copyfile(test_data_file, '%s.gz' % self.wallet_path)
        with gzip.open('%s.gz' % self.wallet_path, 'rb') as rfh:
            wallet_data = rfh.read()
            wallet_data = wallet_data.decode('utf-8')
        with open(self.wallet_path, 'w') as wfh:
            wfh.write(wallet_data)
        self.config = SimpleConfig({'electrum_path': self.user_dir})
        self.config.set_key('dynamic_fees', False, True)
        self.storage = WalletStorage(self.wallet_path)
        self.wallet = Wallet(self.storage)

    def tearDown(self):
        super(TestTxCommandsTestnet, self).tearDown()
        shutil.rmtree(self.user_dir)

    def test_createrawtransaction(self):
        HEX_TX_OUT1 = ('0200000001604079028e98106d5d88525fe42c950c2ef62d75cde8'
                       '91e3d37ac81ed662ce0c0500000000ffffffff0300000000000000'
                       '00116a0f0102030405060708090a0b0c0d0e0f40420f0000000000'
                       '1976a9145f093709af6e9c15b080de359bdb7e78b1dd983088ac80'
                       '841e00000000001976a91491282e4cd7d5e44867f93e8002393dac'
                       '7c2a99db88ac00000000')
        HEX_TX_OUT2 = ('0200000000030000000000000000116a0f0102030405060708090a'
                       '0b0c0d0e0f40420f00000000001976a9145f093709af6e9c15b080'
                       'de359bdb7e78b1dd983088ac80841e00000000001976a91491282e'
                       '4cd7d5e44867f93e8002393dac7c2a99db88ac00000000')
        HEX_TX_OUT3 = ('02000000000000000000')

        inputs = [{'txid': '0cce62d61ec87ad3e391e8cd752df62e'
                           '0c952ce45f52885d6d10988e02794060',
                   'vout': 5}]
        outputs = {'yUyx5hJsEwAukTdRy7UihU57rC37Y4y2ZX': 0.01,
                   'yZYxxqJNR6fJ3fAT4Kyhye3A7G9kC19B9q': '0.02',
                   'data': '0102030405060708090a0b0c0d0e0f'}
        cmds = Commands(config=self.config, wallet=self.wallet, network=None)
        assert cmds.createrawtransaction(inputs, outputs) == HEX_TX_OUT1
        assert cmds.createrawtransaction([], outputs) == HEX_TX_OUT2
        assert cmds.createrawtransaction([], {}) == HEX_TX_OUT3
