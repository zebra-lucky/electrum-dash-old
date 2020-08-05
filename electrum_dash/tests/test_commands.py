import os
import gzip
import unittest
import tempfile
import shutil
from unittest import mock
from decimal import Decimal
from pprint import pprint

from electrum_dash.bitcoin import TYPE_ADDRESS
from electrum_dash.commands import Commands, eval_bool
from electrum_dash import storage
from electrum_dash.simple_config import SimpleConfig
from electrum_dash.storage import WalletStorage
from electrum_dash.transaction import Transaction, TxOutput
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
        self.wallet = w = Wallet(self.storage)
        # set frozen state for small coins
        coins = w.get_spendable_coins(domain=None, config=self.config,
                                      include_ps=True)
        coins = [c for c in coins if c['value'] < 500000000]
        w.set_frozen_state_of_coins(coins, True)

    def tearDown(self):
        super(TestTxCommandsTestnet, self).tearDown()
        shutil.rmtree(self.user_dir)

    def test_createrawtransaction(self):
        inputs = [{'txid': '18c45043a0a24b1a2dc621733b08c220'
                           'd696e0a63b786f4f08206a778cebba8e',
                   'vout': 5}]
        outputs = {'yUyx5hJsEwAukTdRy7UihU57rC37Y4y2ZX': 0.01,
                   'yZYxxqJNR6fJ3fAT4Kyhye3A7G9kC19B9q': '0.02',
                   'data': '0102030405060708090a0b0c0d0e0f'}
        cmds = Commands(config=self.config, wallet=self.wallet, network=None)
        res = cmds.createrawtransaction(inputs, outputs)
        tx = Transaction(res)
        assert len(tx.inputs()) == 1
        assert len(tx.outputs()) == 3
        in0 = tx.inputs()[0]
        assert in0['prevout_hash'] == inputs[0]['txid']
        assert in0['prevout_n'] == inputs[0]['vout']
        assert in0['sequence'] == 0xffffffff
        assert in0['type'] == 'p2pkh'
        assert in0['address'] == 'yRUktd39y5aU3JCgvZSx2NVfwPnv5nB2PF'
        assert in0['num_sig'] == 1
        assert in0['scriptSig'] == ('01ff4c53ff043587cf03ad611a5780000000b8a1'
                                    'b914a2fb35d00a57e9a7773ab382f4cde4001e99'
                                    '8066ba63fc6e0e87cf2302eae8092d829c2290df'
                                    '88271e45b5aec540a5951155cec8929359bacb04'
                                    'acacbb01000000')
        assert in0['x_pubkeys'] == ['ff043587cf03ad611a5780000000b8a1b914a2fb'
                                    '35d00a57e9a7773ab382f4cde4001e998066ba63'
                                    'fc6e0e87cf2302eae8092d829c2290df88271e45'
                                    'b5aec540a5951155cec8929359bacb04acacbb01'
                                    '000000']
        assert in0['pubkeys'] == ['03e8f307174144ef506fe6e5173da6a8'
                                  '7d0864c0d435cda8029ede0df060a5026d']
        assert in0['signatures'] == [None]

        out0 = tx.outputs()[0]
        out1 = tx.outputs()[1]
        out2 = tx.outputs()[2]
        assert out0 == (2, '6a0f0102030405060708090a0b0c0d0e0f', 0)
        assert out1 == (0, 'yUyx5hJsEwAukTdRy7UihU57rC37Y4y2ZX', 1000000)
        assert out2 == (0, 'yZYxxqJNR6fJ3fAT4Kyhye3A7G9kC19B9q', 2000000)
        assert tx.version == 2
        assert tx.tx_type == 0
        assert tx.locktime == 0

        res = cmds.createrawtransaction([], outputs)
        tx = Transaction(res)
        assert len(tx.inputs()) == 0
        assert len(tx.outputs()) == 3
        out0 = tx.outputs()[0]
        out1 = tx.outputs()[1]
        out2 = tx.outputs()[2]
        assert out0 == (2, '6a0f0102030405060708090a0b0c0d0e0f', 0)
        assert out1 == (0, 'yUyx5hJsEwAukTdRy7UihU57rC37Y4y2ZX', 1000000)
        assert out2 == (0, 'yZYxxqJNR6fJ3fAT4Kyhye3A7G9kC19B9q', 2000000)
        assert tx.version == 2
        assert tx.tx_type == 0
        assert tx.locktime == 0

        res = cmds.createrawtransaction([], {})
        tx = Transaction(res)
        assert len(tx.inputs()) == 0
        assert len(tx.outputs()) == 0
        assert tx.version == 2
        assert tx.tx_type == 0
        assert tx.locktime == 0

    def test_addmultisigaddress(self):
        w = self.wallet
        cmds = Commands(config=self.config, wallet=w, network=None)
        myaddr = w.get_unused_addresses()[0]
        mypubk = w.get_public_key(myaddr)
        pubkeys = ['026ca55022fea6b2fe812786760c1ced'
                   'ea68fef642f0cbbf690388e8c1ae20e7a9',
                   '03adc6c1b3ead1e7c1ea601984900329'
                   '230ad282fb1b356a4c8e7b416c6e15d8e9', mypubk]
        res1 = cmds.addmultisigaddress(2, pubkeys)
        msaddr1 = '8xcxHMnsmW9zQHmi8cb6MgyvNsp1E1bAeS'
        redeem_s1 = ('5221026ca55022fea6b2fe812786760c1cedea68fef642f0cbbf690'
                     '388e8c1ae20e7a92103adc6c1b3ead1e7c1ea601984900329230ad2'
                     '82fb1b356a4c8e7b416c6e15d8e92102a60991d30026048b796161d'
                     'd0437cd20e12e7f99b9665ca81bc9594904b2fe2f53ae')
        assert res1 == {'address': msaddr1, 'redeemScript': redeem_s1}

        res2 = cmds.addmultisigaddress(2, pubkeys, True)
        msaddr2 = '8ffQd3r3qT9PQ2knf5nV5KbQihKvNMYrFQ'
        redeem_s2 = ('5221026ca55022fea6b2fe812786760c1cedea68fef642f0cbbf690'
                     '388e8c1ae20e7a92102a60991d30026048b796161dd0437cd20e12e'
                     '7f99b9665ca81bc9594904b2fe2f2103adc6c1b3ead1e7c1ea60198'
                     '4900329230ad282fb1b356a4c8e7b416c6e15d8e953ae')
        assert res2 == {'address': msaddr2, 'redeemScript': redeem_s2}

        assert w.db.get_multisig_imported_addrs() == sorted([msaddr1, msaddr2])

        data1 = w.db.get_multisig_imported_addr(msaddr1)
        data2 = w.db.get_multisig_imported_addr(msaddr2)
        assert data1 == (redeem_s1, 2, pubkeys, myaddr)
        assert data2 == (redeem_s2, 2, sorted(pubkeys), myaddr)

    def with_wallet2(func):
        def setup_wallet2(self, *args, **kwargs):
            tests_path = os.path.dirname(os.path.abspath(__file__))
            w2_data_file = os.path.join(tests_path, 'data', 'w2.gz')
            w2_path = os.path.join(self.user_dir, 'w2')
            shutil.copyfile(w2_data_file, '%s.gz' % w2_path)
            with gzip.open('%s.gz' % w2_path, 'rb') as rfh:
                w2_data = rfh.read()
                w2_data = w2_data.decode('utf-8')
            with open(w2_path, 'w') as wfh:
                wfh.write(w2_data)
            storage2 = WalletStorage(w2_path)
            self.w2 = Wallet(storage2)
            return func(self, *args, **kwargs)
        return setup_wallet2

    def with_wallet2_funded(func):
        def fund_wallet2(self, *args, **kwargs):
            w = self.wallet
            w2 = self.w2
            coins = w.get_spendable_coins(None, self.config)
            outputs = [TxOutput(TYPE_ADDRESS,
                                'yN2ag4KuQvxQLNYTXs32yNpgdLibsn8Y5E',
                                100000000)]
            tx = w.make_unsigned_transaction(coins, outputs, self.config)
            w.sign_transaction(tx, None)
            txid = tx.txid()
            w.add_transaction(txid, tx)
            w2.add_transaction(txid, tx)
            return func(self, *args, **kwargs)
        return fund_wallet2

    def with_get_input_tx_mocked(func):
        def mock_get_input_tx(self, *args, **kwargs):
            w = self.wallet
            w2 = self.w2
            def get_from_both_wallets(txid, spv_verify=True, required_conf=3):
                return (w.db.get_transaction(txid)
                        or w2.db.get_transaction(txid))
            w.get_input_tx = w2.get_input_tx = get_from_both_wallets
            return func(self, *args, **kwargs)
        return mock_get_input_tx

    @with_wallet2
    @with_wallet2_funded
    @with_get_input_tx_mocked
    def test_fundrawtransaction(self):
        w = self.wallet
        w2 = self.w2
        cmds = Commands(config=self.config, wallet=w, network=None)
        w2_cmds = Commands(config=self.config, wallet=w2, network=None)
        outputs = {'yUyx5hJsEwAukTdRy7UihU57rC37Y4y2ZX': 0.3}
        res_tx_hex = cmds.createrawtransaction([], outputs)
        res_tx = Transaction(res_tx_hex)
        assert w.get_tx_vals(res_tx) == ([], [30000000])

        # check fundupto 0
        cmd_opts = {'fundupto': 0, 'outval': 0.3}
        res = cmds.fundrawtransaction(res_tx_hex, cmd_opts)
        res_tx_hex = res['hex']
        res_tx = Transaction(res_tx_hex)
        assert res['fee'] == -29999774   # not enough funded
        assert res['funded_fee'] == 226  # diff in new inputs/outputs values
        assert res['changepos'] == 1
        assert w.get_tx_vals(res_tx) == ([701806547], [30000000, 701806321])

        # check fundupto 0.1
        cmd_opts.update({'fundupto': 0.1})
        res = cmds.fundrawtransaction(res_tx_hex, cmd_opts)
        res_tx_hex = res['hex']
        res_tx = Transaction(res_tx_hex)
        assert res['fee'] == -19999592   # not enough funded
        assert res['funded_fee'] == 408  # diff in new inputs/outputs values
        assert res['changepos'] == 1
        assert w.get_tx_vals(res_tx) == ([701806547, 701806547],
                                         [30000000, 691806365, 701806321])

        # check fundupto 0.2
        cmd_opts.update({'fundupto': 0.2})
        res = w2_cmds.fundrawtransaction(res_tx_hex, cmd_opts)
        res_tx_hex = res['hex']
        res_tx = Transaction(res_tx_hex)
        assert res['fee'] == -9999410    # not enough funded
        assert res['funded_fee'] == 590  # diff in new inputs/outputs values
        assert res['changepos'] == 1
        assert w.get_tx_vals(res_tx) == ([100000000, 701806547, 701806547],
                                         [30000000, 89999818, 691806365,
                                          701806321])
        # check fundupto 0.3
        cmd_opts.update({'fundupto': 0.3})
        res = cmds.fundrawtransaction(res_tx_hex, cmd_opts)
        res_tx_hex = res['hex']
        res_tx = Transaction(res_tx_hex)
        assert res['fee'] == 772
        assert res['funded_fee'] == 772
        assert res['changepos'] == -1
        assert w.get_tx_vals(res_tx) == ([100000000, 701806547, 701806547,
                                          701806547],
                                         [30000000, 89999818, 691806365,
                                          691806365, 701806321])

    @with_wallet2
    @with_wallet2_funded
    @with_get_input_tx_mocked
    def test_signtransaction_with_multiwallet_inputs(self):
        w = self.wallet
        w2 = self.w2
        cmds = Commands(config=self.config, wallet=w, network=None)
        w2_cmds = Commands(config=self.config, wallet=w2, network=None)
        outputs = {'yUyx5hJsEwAukTdRy7UihU57rC37Y4y2ZX': 0.2}

        res_tx_hex = cmds.createrawtransaction([], outputs)
        # check fundupto 0.1 from w
        cmd_opts = {'fundupto': 0.1, 'outval': 0.2}
        res = cmds.fundrawtransaction(res_tx_hex, cmd_opts)
        #assert res['fee'] == -9999626    # not enough funded
        #assert res['funded_fee'] == 374 # diff in new inputs/outputs values
        #assert res['changepos'] == 1
        res_tx_hex = res['hex']
        res_tx = Transaction(res_tx_hex)
        #assert w.get_tx_vals(res_tx) == ([30000000],
        #                                 [20000000, 19999774])
        # check fundupto 0.2 from w2
        cmd_opts.update({'fundupto': 0.2})
        res = w2_cmds.fundrawtransaction(res_tx_hex, cmd_opts)
        #assert res['fee'] == 556
        #assert res['funded_fee'] == 556  # diff in new inputs/outputs values
        #assert res['changepos'] == 1
        res_tx_hex = res['hex']
        res_tx = Transaction(res_tx_hex)
        #assert w.get_tx_vals(res_tx) == ([100000000, 10000100, 10000100],
        #                                 [9999826, 20000000, 89999818])

        print('1'*10)
        pprint(res_tx.inputs())
        pprint(res_tx.outputs())
        res = cmds.signtransaction(res_tx_hex)
        print('2'*10)
        pprint(res)
        res_tx_hex = res['hex']
        res = w2_cmds.signtransaction(res_tx_hex)
        print('3'*10)
        pprint(res)
        assert 0
