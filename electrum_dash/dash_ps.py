import asyncio
import copy
import logging
import re
import random
import time
import threading
from bls_py import bls
from enum import IntEnum
from collections import defaultdict, deque
from decimal import Decimal
from uuid import uuid4

from .bitcoin import COIN, TYPE_ADDRESS, TYPE_SCRIPT, address_to_script
from .dash_tx import (STANDARD_TX, PSTxTypes, SPEC_TX_NAMES, PSCoinRounds,
                      str_ip, CTxIn, CTxOut)
from .dash_msg import (DSPoolStatusUpdate, DSMessageIDs, ds_msg_str,
                       ds_pool_state_str, DashDsaMsg, DashDsiMsg, DashDssMsg,
                       PRIVATESEND_ENTRY_MAX_SIZE)
from .logging import Logger, root_logger
from .transaction import Transaction, TxOutput
from .util import (NoDynamicFeeEstimates, log_exceptions, SilentTaskGroup,
                   NotEnoughFunds, bfh, is_android)
from .i18n import _


TXID_PATTERN = re.compile('([0123456789ABCDEFabcdef]{64})')
DUMMY_TXID = '_DUMMY_TXID_'


def filter_log_line(line):
    pos = 0
    output_line = ''
    while pos < len(line):
        m = TXID_PATTERN.search(line, pos)
        if not m:
            output_line += line[pos:]
            break
        output_line += line[pos:m.start()]
        output_line += DUMMY_TXID
        pos = m.end()
    return output_line


def to_duffs(amount):
    return round(Decimal(amount)*COIN)


def sort_utxos_by_ps_rounds(x):
    ps_rounds = x['ps_rounds']
    if ps_rounds is None:
        return PSCoinRounds.MINUSINF
    return ps_rounds


class PSDenoms(IntEnum):
    D10 = 1
    D1 = 2
    D0_1 = 4
    D0_01 = 8
    D0_001 = 16


PS_DENOMS_DICT = {
    to_duffs(10.0001): PSDenoms.D10,
    to_duffs(1.00001): PSDenoms.D1,
    to_duffs(0.100001): PSDenoms.D0_1,
    to_duffs(0.0100001): PSDenoms.D0_01,
    to_duffs(0.00100001): PSDenoms.D0_001,
}


PS_DENOM_REVERSE_DICT = {int(v): k for k, v in PS_DENOMS_DICT.items()}


COLLATERAL_VAL = to_duffs(0.0001)
CREATE_COLLATERAL_VAL = COLLATERAL_VAL*4
PS_DENOMS_VALS = sorted(PS_DENOMS_DICT.keys())

PS_MIXING_TX_TYPES = list(map(lambda x: x.value, [PSTxTypes.NEW_DENOMS,
                                                  PSTxTypes.NEW_COLLATERAL,
                                                  PSTxTypes.PAY_COLLATERAL,
                                                  PSTxTypes.DENOMINATE]))

PS_SAVED_TX_TYPES = list(map(lambda x: x.value, [PSTxTypes.NEW_DENOMS,
                                                 PSTxTypes.NEW_COLLATERAL,
                                                 PSTxTypes.PAY_COLLATERAL,
                                                 PSTxTypes.DENOMINATE,
                                                 PSTxTypes.PRIVATESEND,
                                                 PSTxTypes.SPEND_PS_COINS,
                                                 PSTxTypes.OTHER_PS_COINS]))

DEFAULT_KEEP_AMOUNT = 2
MIN_KEEP_AMOUNT = 2
MAX_KEEP_AMOUNT = 1000

DEFAULT_MIX_ROUNDS = 4
MIN_MIX_ROUNDS = 2
MAX_MIX_ROUNDS = 16

DEFAULT_PRIVATESEND_DENOMS = 300
MIN_PRIVATESEND_DENOMS = 10
MAX_PRIVATESEND_DENOMS = 100000

DEFAULT_PRIVATESEND_SESSIONS = 4
MIN_PRIVATESEND_SESSIONS = 1
MAX_PRIVATESEND_SESSIONS = 10

DEFAULT_GROUP_HISTORY = True
DEFAULT_NOTIFY_PS_TXS = False
DEFAULT_SUBSCRIBE_SPENT = False

POOL_MIN_PARTICIPANTS = 3
POOL_MAX_PARTICIPANTS = 5

PRIVATESEND_QUEUE_TIMEOUT = 30
PRIVATESEND_DSQ_TIMEOUT = 40

AFTER_MIXING_STOP_WAIT_TIME = 60

DEFAULT_NUM_KEYS_TO_CACHE = 1000
MIN_NUM_KEYS_TO_CACHE = 500
MAX_NUM_KEYS_TO_CACHE = 5000


class PSTxData:
    '''
    uuid: unique id for addresses reservation
    tx_type: PSTxTypes type
    txid: tx hash
    raw_tx: raw tx data
    sent: time when tx was sent to network
    next_send: minimal time when next send attempt should occur
    '''

    __slots__ = 'uuid tx_type txid raw_tx sent next_send'.split()

    def __init__(self, **kwargs):
        for k in self.__slots__:
            if k in kwargs:
                if k == 'tx_type':
                    setattr(self, k, int(kwargs[k]))
                else:
                    setattr(self, k, kwargs[k])
            else:
                setattr(self, k, None)

    def _as_dict(self):
        '''return dict txid -> (uuid, sent, next_send, tx_type, raw_tx)'''
        return {self.txid: (self.uuid, self.sent, self.next_send,
                            self.tx_type, self.raw_tx)}

    @classmethod
    def _from_txid_and_tuple(cls, txid, data_tuple):
        '''
        New instance from txid
        and (uuid, sent, next_send, tx_type, raw_tx) tuple
        '''
        uuid, sent, next_send, tx_type, raw_tx = data_tuple
        return cls(uuid=uuid, txid=txid, raw_tx=raw_tx,
                   tx_type=tx_type, sent=sent, next_send=next_send)

    def __eq__(self, other):
        if type(other) != PSTxData:
            return False
        if id(self) == id(other):
            return True
        for k in self.__slots__:
            if getattr(self, k) != getattr(other, k):
                return False
        return True

    async def send(self, psman, ignore_next_send=False):
        err = ''
        if self.sent:
            return False, err
        now = time.time()
        if not ignore_next_send:
            next_send = self.next_send
            if next_send and next_send > now:
                return False, err
        try:
            tx = Transaction(self.raw_tx)
            await psman.network.broadcast_transaction(tx)
            self.sent = time.time()
            return True, err
        except Exception as e:
            err = str(e)
            self.next_send = now + 10
            return False, err


class PSTxWorkflow:
    '''
    uuid: unique id for addresses reservation
    completed: workflow creation completed
    tx_data: txid -> PSTxData
    tx_order: creation order of workflow txs
    '''

    __slots__ = 'uuid completed tx_data tx_order'.split()

    def __init__(self, **kwargs):
        uuid = kwargs.pop('uuid', None)
        if uuid is None:
            raise TypeError('missing required uuid argument')
        self.uuid = uuid
        self.completed = kwargs.pop('completed', False)
        self.tx_order = kwargs.pop('tx_order', [])[:]  # copy
        tx_data = kwargs.pop('tx_data', {})
        self.tx_data = {}  # copy
        for txid, v in tx_data.items():
            if type(v) in (tuple, list):
                self.tx_data[txid] = PSTxData._from_txid_and_tuple(txid, v)
            else:
                self.tx_data[txid] = v

    def _as_dict(self):
        '''return dict with keys from __slots__ and corresponding values'''
        tx_data = {}  # copy
        for v in self.tx_data.values():
            tx_data.update(v._as_dict())
        return {
            'uuid': self.uuid,
            'completed': self.completed,
            'tx_data': tx_data,
            'tx_order': self.tx_order[:],  # copy
        }

    @classmethod
    def _from_dict(cls, data_dict):
        return cls(**data_dict)

    def __eq__(self, other):
        if type(other) != PSTxWorkflow:
            return False
        elif id(self) == id(other):
            return True
        elif self.uuid != other.uuid:
            return False
        elif self.completed != other.completed:
            return False
        elif self.tx_order != other.tx_order:
            return False
        elif set(self.tx_data.keys()) != set(other.tx_data.keys()):
            return False
        for k in self.tx_data.keys():
            if self.tx_data[k] != other.tx_data[k]:
                return False
        else:
            return True

    def next_to_send(self, wallet):
        for txid in self.tx_order:
            tx_data = self.tx_data[txid]
            if not tx_data.sent and wallet.is_local_tx(txid):
                return tx_data

    def add_tx(self, **kwargs):
        txid = kwargs.pop('txid')
        raw_tx = kwargs.pop('raw_tx', None)
        tx_type = kwargs.pop('tx_type')
        if not txid or not tx_type:
            return
        tx_data = PSTxData(uuid=self.uuid, txid=txid,
                           raw_tx=raw_tx, tx_type=tx_type)
        self.tx_data[txid] = tx_data
        self.tx_order.append(txid)
        return tx_data

    def pop_tx(self, txid):
        if txid in self.tx_data:
            res = self.tx_data.pop(txid)
        else:
            res = None
        self.tx_order = [tid for tid in self.tx_order if tid != txid]
        return res


class PSDenominateWorkflow:
    '''
    uuid: unique id for spending denoms reservation
    denom: workflow denom value
    rounds: workflow inputs mix rounds
    inputs: list of spending denoms outpoints
    outputs: list of reserved output addresses
    completed: worfklow completed after dsc message received
    '''

    __slots__ = 'uuid denom rounds inputs outputs completed'.split()

    def __init__(self, **kwargs):
        uuid = kwargs.pop('uuid', None)
        if uuid is None:
            raise TypeError('missing required uuid argument')
        self.uuid = uuid
        self.denom = kwargs.pop('denom', 0)
        self.rounds = kwargs.pop('rounds', 0)
        self.inputs = kwargs.pop('inputs', [])[:]  # copy
        self.outputs = kwargs.pop('outputs', [])[:]  # copy
        self.completed = kwargs.pop('completed', False)

    def _as_dict(self):
        '''return dict uuid -> (denom, rounds, inputs, outputs, completed)'''
        return {
            self.uuid: (
                self.denom,
                self.rounds,
                self.inputs[:],  # copy
                self.outputs[:],  # copy
                self.completed,
            )
        }

    @classmethod
    def _from_uuid_and_tuple(cls, uuid, data_tuple):
        '''
        New from uuid and tuple of (denom, rounds, inputs, outputs, completed)
        '''
        denom, rounds, inputs, outputs, completed = data_tuple
        return cls(uuid=uuid, denom=denom, rounds=rounds,
                   inputs=inputs[:], outputs=outputs[:],  # copy
                   completed=completed)

    def __eq__(self, other):
        if type(other) != PSDenominateWorkflow:
            return False
        elif id(self) == id(other):
            return True
        elif self.uuid != other.uuid:
            return False
        elif self.denom != other.denom:
            return False
        elif self.rounds != other.rounds:
            return False
        elif self.inputs != other.inputs:
            return False
        elif self.outputs != other.outputs:
            return False
        elif self.completed != other.completed:
            return False
        else:
            return True


class PSMinRoundsCheckFailed(Exception):
    """Thrown when check for coins minimum mixing rounds failed"""


class PSPossibleDoubleSpendError(Exception):
    """Thrown when trying to broadcast recently used ps denoms/collateral"""


class PSSpendToPSAddressesError(Exception):
    """Thrown when trying to broadcast tx with ps coins spent to ps addrs"""


class SignWithKeyipairsFailed(Exception):
    """Thrown when transaction signing with keypairs reserved failed"""


class AddPSDataError(Exception):
    """Thrown when failed _add_*_ps_data method"""


class RmPSDataError(Exception):
    """Thrown when failed _rm_*_ps_data method"""


class PSMixSession(Logger):

    LOGGING_SHORTCUT = 'A'

    def __init__(self, psman, denom_value, denom, dsq):
        self.denom_value = denom_value
        self.denom = denom

        network = psman.wallet.network
        self.dash_net = network.dash_net
        self.mn_list = network.mn_list

        self.dash_peer = None
        self.sml_entry = None

        if dsq:
            outpoint = str(dsq.masternodeOutPoint)
            self.sml_entry = self.mn_list.get_mn_by_outpoint(outpoint)
        if not self.sml_entry:
            self.sml_entry = self.mn_list.get_random_mn()
        if not self.sml_entry:
            raise Exception('No SML entries found')
        psman.recent_mixes_mns.append(self.peer_str)
        Logger.__init__(self)

        self.msg_queue = asyncio.Queue()

        self.session_id = 0
        self.state = None
        self.msg_id = None
        self.entries_count = 0
        self.masternodeOutPoint = None
        self.fReady = False
        self.nTime = 0
        self.start_time = time.time()

    @property
    def peer_str(self):
        return f'{str_ip(self.sml_entry.ipAddress)}:{self.sml_entry.port}'

    def diagnostic_name(self):
        return self.peer_str

    async def run_peer(self):
        if self.dash_peer:
            raise Exception('Sesions already have running DashPeer')
        self.dash_peer = await self.dash_net.run_mixing_peer(self.peer_str,
                                                             self.sml_entry,
                                                             self)
        if not self.dash_peer:
            raise Exception(f'Peer {self.peer_str} connection failed')
        self.logger.info(f'Started mixing session {self.peer_str},'
                         f' denom={self.denom_value} (nDenom={self.denom})')

    def close_peer(self):
        if not self.dash_peer:
            return
        self.dash_peer.close()
        self.logger.info(f'Stopped mixing session {self.peer_str}')

    def verify_ds_msg_sig(self, ds_msg):
        if not self.sml_entry:
            return False
        mn_pub_key = self.sml_entry.pubKeyOperator
        pubk = bls.PublicKey.from_bytes(mn_pub_key)
        sig = bls.Signature.from_bytes(ds_msg.vchSig)
        msg_hash = ds_msg.msg_hash()
        aggr_info = bls.AggregationInfo.from_msg_hash(pubk, msg_hash)
        sig.set_aggregation_info(aggr_info)
        return bls.BLS.verify(sig)

    def verify_final_tx(self, tx, denominate_wfl):
        inputs = denominate_wfl.inputs
        outputs = denominate_wfl.outputs
        icnt = 0
        ocnt = 0
        for i in tx.inputs():
            prev_h = i['prevout_hash']
            prev_n = i['prevout_n']
            if f'{prev_h}:{prev_n}' in inputs:
                icnt += 1
        for o in tx.outputs():
            if o.address in outputs:
                ocnt += 1
        if icnt == len(inputs) and ocnt == len(outputs):
            return True
        else:
            return False

    async def send_dsa(self, pay_collateral_tx):
        msg = DashDsaMsg(self.denom, pay_collateral_tx)
        await self.dash_peer.send_msg('dsa', msg.serialize())
        self.logger.debug(f'dsa sent')

    async def send_dsi(self, inputs, pay_collateral_tx, outputs):
        scriptSig = b''
        sequence = 0xffffffff
        vecTxDSIn = []
        for i in inputs:
            prev_h, prev_n = i.split(':')
            prev_h = bfh(prev_h)[::-1]
            prev_n = int(prev_n)
            vecTxDSIn.append(CTxIn(prev_h, prev_n, scriptSig, sequence))
        vecTxDSOut = []
        for o in outputs:
            scriptPubKey = bfh(address_to_script(o))
            vecTxDSOut.append(CTxOut(self.denom_value, scriptPubKey))
        msg = DashDsiMsg(vecTxDSIn, pay_collateral_tx, vecTxDSOut)
        await self.dash_peer.send_msg('dsi', msg.serialize())
        self.logger.debug(f'dsi sent')

    async def send_dss(self, signed_inputs):
        msg = DashDssMsg(signed_inputs)
        await self.dash_peer.send_msg('dss', msg.serialize())

    async def read_next_msg(self, denominate_wfl, timeout=None):
        '''Read next msg from msg_queue, process and return (cmd, res) tuple'''
        try:
            if timeout is None:
                timeout = 120
            dash_cmd = await asyncio.wait_for(self.msg_queue.get(), timeout)
        except asyncio.TimeoutError:
            raise Exception('Session Timeout, Reset')
        if not dash_cmd:  # dash_peer is closed
            return None, None
        cmd = dash_cmd.cmd
        payload = dash_cmd.payload
        if cmd == 'dssu':
            res = self.on_dssu(payload)
            return cmd, res
        elif cmd == 'dsq':
            self.logger.debug(f'dsq read: {payload}')
            res = self.on_dsq(payload)
            return cmd, res
        elif cmd == 'dsf':
            self.logger.debug(f'dsf read: {payload}')
            res = self.on_dsf(payload, denominate_wfl)
            return cmd, res
        elif cmd == 'dsc':
            self.logger.debug(f'dsc read: {payload}')
            res = self.on_dsc(payload)
            return cmd, res
        else:
            self.logger.debug(f'unknown msg read, cmd: {cmd}')
            return None, None

    def on_dssu(self, dssu):
        session_id = dssu.sessionID
        if not self.session_id:
            if session_id:
                self.session_id = session_id

        if self.session_id != session_id:
            raise Exception(f'Wrong session id {session_id},'
                            f' was {self.session_id}')

        self.state = dssu.state
        self.msg_id = dssu.messageID
        self.entries_count = dssu.entriesCount

        state = ds_pool_state_str(self.state)
        msg = ds_msg_str(self.msg_id)
        if (dssu.statusUpdate == DSPoolStatusUpdate.ACCEPTED
                and dssu.messageID != DSMessageIDs.ERR_QUEUE_FULL):
            self.logger.debug(f'dssu read: state={state}, msg={msg},'
                              f' entries_count={self.entries_count}')
        elif dssu.statusUpdate == DSPoolStatusUpdate.ACCEPTED:
            raise Exception('MN queue is full')
        elif dssu.statusUpdate == DSPoolStatusUpdate.REJECTED:
            raise Exception(f'Get reject status update from MN: {msg}')
        else:
            raise Exception(f'Unknown dssu statusUpdate: {dssu.statusUpdate}')

    def on_dsq(self, dsq):
        denom = dsq.nDenom
        if denom != self.denom:
            raise Exception(f'Wrong denom in dsq msg: {denom},'
                            f' session denom is {self.denom}.')
        self.masternodeOutPoint = dsq.masternodeOutPoint
        self.fReady = dsq.fReady
        self.nTime = dsq.nTime

    def on_dsf(self, dsf, denominate_wfl):
        session_id = dsf.sessionID
        if self.session_id != session_id:
            raise Exception(f'Wrong session id {session_id},'
                            f' was {self.session_id}')
        if not self.verify_final_tx(dsf.txFinal, denominate_wfl):
            raise Exception(f'Wrong txFinal')
        return dsf.txFinal

    def on_dsc(self, dsc):
        msg_id = dsc.messageID
        if msg_id != DSMessageIDs.MSG_SUCCESS:
            raise Exception(ds_msg_str(msg_id))


class PSManagerLogHandler(logging.Handler):
    '''Write log to maxsize limited queue'''

    def __init__(self, psman):
        logging.Handler.__init__(self)
        self.shortcut = psman.LOGGING_SHORTCUT
        self.psman = psman
        self.log_deque = deque([], 500)
        self.setLevel(logging.INFO)
        fmt = '%(asctime)22s | %(message)s'
        self.setFormatter(logging.Formatter(fmt=fmt))
        root_logger.addHandler(self)

    def handle(self, record):
        if getattr(record, 'custom_shortcut', None) != self.shortcut:
            return False
        psman = self.psman
        self.acquire()
        try:
            log_line = ''
            log_line = self.format(record)
            self.log_deque.append(log_line)
        finally:
            self.release()
            if psman.debug and log_line:
                psman.trigger_callback('log-line', psman, log_line)
        return True

    def clear_log(self):
        self.log_deque.clear()

    def get_log(self):
        return '\n'.join(self.log_deque)


class PSManager(Logger):
    '''Class representing wallet PrivateSend manager'''

    LOGGING_SHORTCUT = 'A'
    NOT_ENOUGH_KEYS_MSG = _('Insufficient keypairs cached to continue mixing.'
                            ' You can restart mixing to reserve more keyparis')
    ADD_PS_DATA_ERR_MSG = _('Error on adding PrivateSend transaction data.')
    SPEND_TO_PS_ADDRS_MSG = _('For privacy reasons blocked attempt to'
                              ' transfer coins to PrivateSend address.')
    ALL_MIXED_MSG = _('PrivateSend mixing is done')
    CLEAR_PS_DATA_MSG = _('Are you sure to clear all wallet PrivateSend data?'
                          ' This is not recommended if there is'
                          ' no particular need.')
    LLMQ_DATA_NOT_READY = _('LLMQ quorums data is not fully loaded.'
                            ' Please try again soon.')
    MNS_DATA_NOT_READY = _('Masternodes data is not fully loaded.'
                           ' Please try again soon.')
    if is_android():
        NO_DYNAMIC_FEE_MSG = _('{}\n\nYou can switch fee estimation method'
                               ' on send screen')
    else:
        NO_DYNAMIC_FEE_MSG = _('{}\n\nYou can switch to static fee estimation'
                               ' on Fees Preferences tab')

    def __init__(self, wallet):
        self._debug = False
        Logger.__init__(self)
        self.log_handler = PSManagerLogHandler(self)
        self.wallet = wallet
        self.config = None
        self.wallet_types_supported = ['standard']
        self.enabled = (wallet.wallet_type in self.wallet_types_supported)
        if not self.enabled:
            supported_str = ', '.join(self.wallet_types_supported)
            this_type = wallet.wallet_type
            self.disabled_msg = _(f'PrivateSend is currently supported on'
                                  f' next wallet types: {supported_str}.'
                                  f'\n\nThis wallet has type: {this_type}.')
        else:
            self.disabled_msg  = ''
        self.network = None
        self.dash_net = None
        self.loop = None
        self._loop_thread = None
        self.main_taskgroup = None

        self._is_mixing_run = False
        self._is_mixing_changes = False
        self._keypairs_cache = {}
        self._mix_start_time = None
        self._mix_stop_time = None

        self.callback_lock = threading.Lock()
        self.callbacks = defaultdict(list)

        self.mix_sessions_lock = asyncio.Lock()
        self.mix_sessions = {}  # dict peer -> PSMixSession
        self.recent_mixes_mns = deque([], 16)  # added from mixing sessions

        self.denoms_lock = threading.Lock()
        self.collateral_lock = threading.Lock()
        self.others_lock = threading.Lock()
        self.reserved_lock = threading.Lock()

        self.new_denoms_wfl_lock = threading.Lock()
        self.new_collateral_wfl_lock = threading.Lock()
        self.pay_collateral_wfl_lock = threading.Lock()
        self.denominate_wfl_lock = threading.Lock()

        # _ps_denoms_amount_cache recalculated in add_ps_denom/pop_ps_denom
        self._ps_denoms_amount_cache = 0
        denoms = wallet.db.get_ps_denoms()
        for addr, value, rounds in denoms.values():
            self._ps_denoms_amount_cache += value
        # _denoms_to_mix_cache recalculated on mix_rounds change and
        # in add[_mixing]_denom/pop[_mixing]_denom methods
        self._denoms_to_mix_cache = self.denoms_to_mix()

        # sycnhronizer unsubsribed addresses
        self.spent_addrs = set()
        self.unsubscribed_addrs = set()

    def load_and_cleanup(self):
        w = self.wallet
        # load and unsubscribe spent ps addresses
        unspent = w.db.get_unspent_ps_addresses()
        for addr in w.db.get_ps_addresses():
            if addr in unspent:
                continue
            self.spent_addrs.add(addr)
            if self.subscribe_spent:
                continue
            hist = w.db.get_addr_history(addr)
            self.unsubscribe_spent_addr(addr, hist)
        self.check_ps_txs()

    def check_ps_txs(self):
        w = self.wallet
        ps_txs = w.db.get_ps_txs()
        ps_txs_removed = w.db.get_ps_txs_removed()
        found = 0
        for txid, (tx_type, completed) in ps_txs.items():
            if completed:
                continue
            tx = w.db.get_transaction(txid)
            if tx:
                try:
                    self.logger.info(f'check_ps_txs add {txid} ps data')
                    self._add_ps_data(txid, tx, tx_type)
                    found += 1
                except Exception as e:
                    str_err = f'_add_ps_data {txid} failed: {str(e)}'
                    self.logger.info(str_err)
                    return str_err
        for txid, (tx_type, completed) in ps_txs_removed.items():
            if completed:
                continue
            tx = w.db.get_transaction(txid)
            if tx:
                try:
                    self.logger.info(f'check_ps_txs rm {txid} ps data')
                    self._rm_ps_data(txid, tx, tx_type)
                    found += 1
                except Exception as e:
                    str_err = f'_rm_ps_data {txid} failed: {str(e)}'
                    self.logger.info(str_err)
                    return str_err
        if found:
            self.trigger_callback('ps-data-updated', w)

    @property
    def debug(self):
        return self._debug

    @debug.setter
    def debug(self, debug):
        if self._debug == debug:
            return
        self._debug = debug

    def register_callback(self, callback, events):
        with self.callback_lock:
            for event in events:
                self.callbacks[event].append(callback)

    def unregister_callback(self, callback):
        with self.callback_lock:
            for callbacks in self.callbacks.values():
                if callback in callbacks:
                    callbacks.remove(callback)

    def trigger_callback(self, event, *args):
        try:
            with self.callback_lock:
                callbacks = self.callbacks[event][:]
            [callback(event, *args) for callback in callbacks]
        except Exception as e:
            self.logger.debug(f'Error in trigger_callback: {str(e)}')

    def on_network_start(self, network):
        self.network = network
        self.dash_net = network.dash_net
        self.loop = network.asyncio_loop
        self._loop_thread = network._loop_thread

    async def broadcast_transaction(self, tx, *, timeout=None) -> None:
        if self.enabled:
            w = self.wallet

            def check_spend_to_ps_addresses():
                for o in tx.outputs():
                    addr = o.address
                    if not w.is_mine(addr):
                        continue
                    if addr in w.db.get_ps_addresses():
                        msg = self.SPEND_TO_PS_ADDRS_MSG
                        raise PSSpendToPSAddressesError(msg)
            await self.loop.run_in_executor(None, check_spend_to_ps_addresses)

            def check_possible_dspend():
                with self.denoms_lock, self.collateral_lock:
                    warn = self.double_spend_warn
                    if not warn:
                        return
                    for txin in tx.inputs():
                        prev_h = txin['prevout_hash']
                        prev_n = txin['prevout_n']
                        outpoint = f'{prev_h}:{prev_n}'
                        if (w.db.get_ps_spending_collateral(outpoint)
                                or w.db.get_ps_spending_denom(outpoint)):
                            raise PSPossibleDoubleSpendError(warn)
            await self.loop.run_in_executor(None, check_possible_dspend)
        await self.network.broadcast_transaction(tx, timeout=timeout)

    @property
    def keep_amount(self):
        return self.wallet.db.get_ps_data('keep_amount', DEFAULT_KEEP_AMOUNT)

    @keep_amount.setter
    def keep_amount(self, amount):
        if self.is_mixing_run:
            return
        if self.keep_amount == amount:
            return
        amount = max(MIN_KEEP_AMOUNT, int(amount))
        amount = min(MAX_KEEP_AMOUNT, int(amount))
        self.wallet.db.set_ps_data('keep_amount', amount)

    def keep_amount_data(self, minv=None, maxv=None,
                         short_txt=None, full_txt=None):
        if minv:
            return MIN_KEEP_AMOUNT
        elif maxv:
            return MAX_KEEP_AMOUNT
        elif short_txt:
            return _('Amount of Dash to keep anonymized')
        elif full_txt:
            return _('This amount acts as a threshold to turn off'
                     " PrivateSend mixing once it's reached.")

    @property
    def mix_rounds(self):
        return self.wallet.db.get_ps_data('mix_rounds', DEFAULT_MIX_ROUNDS)

    @mix_rounds.setter
    def mix_rounds(self, rounds):
        if self.is_mixing_run:
            return
        if self.mix_rounds == rounds:
            return
        rounds = max(MIN_MIX_ROUNDS, int(rounds))
        rounds = min(MAX_MIX_ROUNDS, int(rounds))
        self.wallet.db.set_ps_data('mix_rounds', rounds)
        with self.denoms_lock:
            self._denoms_to_mix_cache = self.denoms_to_mix()

    def mix_rounds_data(self, minv=None, maxv=None,
                        short_txt=None, full_txt=None):
        if minv:
            return MIN_MIX_ROUNDS
        elif maxv:
            return MAX_MIX_ROUNDS
        elif short_txt:
            return _('PrivateSend rounds to use')
        elif full_txt:
            return _('This setting determines the amount of individual'
                     ' masternodes that a input will be anonymized through.'
                     ' More rounds of anonymization gives a higher degree'
                     ' of privacy, but also costs more in fees.')

    @property
    def group_history(self):
        return self.wallet.db.get_ps_data('group_history',
                                          DEFAULT_GROUP_HISTORY)

    @group_history.setter
    def group_history(self, group_history):
        if self.group_history == group_history:
            return
        self.wallet.db.set_ps_data('group_history', bool(group_history))

    def group_history_data(self, short_txt=None, full_txt=None):
        if short_txt:
            return _('Group PrivateSend transactions')
        elif full_txt:
            return _('Group PrivateSend mixing transactions in wallet history')

    @property
    def notify_ps_txs(self):
        return self.wallet.db.get_ps_data('notify_ps_txs',
                                          DEFAULT_NOTIFY_PS_TXS)

    @notify_ps_txs.setter
    def notify_ps_txs(self, notify_ps_txs):
        if self.notify_ps_txs == notify_ps_txs:
            return
        self.wallet.db.set_ps_data('notify_ps_txs', bool(notify_ps_txs))

    def notify_ps_txs_data(self, short_txt=None, full_txt=None):
        if short_txt:
            return _('Notify on PrivateSend transactions')
        elif full_txt:
            return _('Notify when PrivateSend mixing transactions is arrived')

    def need_notify(self, txid):
        if self.notify_ps_txs:
            return True
        tx_type, completed = self.wallet.db.get_ps_tx(txid)
        if tx_type not in PS_MIXING_TX_TYPES:
            return True
        else:
            return False

    @property
    def max_sessions(self):
        return self.wallet.db.get_ps_data('max_sessions',
                                          DEFAULT_PRIVATESEND_SESSIONS)

    @max_sessions.setter
    def max_sessions(self, max_sessions):
        if self.max_sessions == max_sessions:
            return
        self.wallet.db.set_ps_data('max_sessions', int(max_sessions))

    def max_sessions_data(self, minv=None, maxv=None,
                          short_txt=None, full_txt=None):
        if minv:
            return MIN_PRIVATESEND_SESSIONS
        elif maxv:
            return MAX_PRIVATESEND_SESSIONS
        elif short_txt:
            return _('PrivateSend sessions')
        elif full_txt:
            return _('Count of PrivateSend mixing session')

    @property
    def subscribe_spent(self):
        return self.wallet.db.get_ps_data('subscribe_spent',
                                          DEFAULT_SUBSCRIBE_SPENT)

    @subscribe_spent.setter
    def subscribe_spent(self, subscribe_spent):
        if self.subscribe_spent == subscribe_spent:
            return
        self.wallet.db.set_ps_data('subscribe_spent', bool(subscribe_spent))
        w = self.wallet
        if subscribe_spent:
            for addr in self.spent_addrs:
                self.subscribe_spent_addr(addr)
        else:
            for addr in self.spent_addrs:
                hist = w.db.get_addr_history(addr)
                self.unsubscribe_spent_addr(addr, hist)

    def subscribe_spent_data(self, short_txt=None, full_txt=None):
        if short_txt:
            return _('Subscribe to spent PS addresses')
        elif full_txt:
            return _('Subscribe to spent PS addresses'
                     ' on electrum servers')

    @property
    def num_keys_to_cache(self):
        return self.wallet.db.get_ps_data('num_keys_to_cache',
                                          DEFAULT_NUM_KEYS_TO_CACHE)

    @num_keys_to_cache.setter
    def num_keys_to_cache(self, num_keys_to_cache):
        if self.num_keys_to_cache == num_keys_to_cache:
            return
        self.wallet.db.set_ps_data('num_keys_to_cache', int(num_keys_to_cache))

    @property
    def ps_collateral_cnt(self):
        return len(self.wallet.db.get_ps_collaterals())

    def add_ps_spending_collateral(self, outpoint, wfl_uuid):
        self.wallet.db._add_ps_spending_collateral(outpoint, wfl_uuid)

    def pop_ps_spending_collateral(self, outpoint):
        return self.wallet.db._pop_ps_spending_collateral(outpoint)

    def add_ps_denom(self, outpoint, denom):  # denom is (addr, value, rounds)
        self.wallet.db._add_ps_denom(outpoint, denom)
        self._ps_denoms_amount_cache += denom[1]
        if denom[2] < self.mix_rounds:  # if rounds < mix_rounds
            self._denoms_to_mix_cache[outpoint] = denom

    def pop_ps_denom(self, outpoint):
        denom = self.wallet.db._pop_ps_denom(outpoint)
        if denom:
            self._ps_denoms_amount_cache -= denom[1]
            self._denoms_to_mix_cache.pop(outpoint, None)
        return denom

    def add_ps_spending_denom(self, outpoint, wfl_uuid):
        self.wallet.db._add_ps_spending_denom(outpoint, wfl_uuid)
        self._denoms_to_mix_cache.pop(outpoint, None)

    def pop_ps_spending_denom(self, outpoint):
        db = self.wallet.db
        denom = db.get_ps_denom(outpoint)
        if denom and denom[2] < self.mix_rounds:  # if rounds < mix_rounds
            self._denoms_to_mix_cache[outpoint] = denom
        return db._pop_ps_spending_denom(outpoint)

    @property
    def pay_collateral_wfl(self):
        d = self.wallet.db.get_ps_data('pay_collateral_wfl')
        if d:
            return PSTxWorkflow._from_dict(d)

    def set_pay_collateral_wfl(self, workflow):
        self.wallet.db.set_ps_data('pay_collateral_wfl', workflow._as_dict())

    def clear_pay_collateral_wfl(self):
        self.wallet.db.set_ps_data('pay_collateral_wfl', {})

    @property
    def new_collateral_wfl(self):
        d = self.wallet.db.get_ps_data('new_collateral_wfl')
        if d:
            return PSTxWorkflow._from_dict(d)

    def set_new_collateral_wfl(self, workflow):
        self.wallet.db.set_ps_data('new_collateral_wfl', workflow._as_dict())

    def clear_new_collateral_wfl(self):
        self.wallet.db.set_ps_data('new_collateral_wfl', {})

    @property
    def new_denoms_wfl(self):
        d = self.wallet.db.get_ps_data('new_denoms_wfl')
        if d:
            return PSTxWorkflow._from_dict(d)

    def set_new_denoms_wfl(self, workflow):
        self.wallet.db.set_ps_data('new_denoms_wfl', workflow._as_dict())

    def clear_new_denoms_wfl(self):
        self.wallet.db.set_ps_data('new_denoms_wfl', {})

    @property
    def denominate_wfl_list(self):
        wfls = self.wallet.db.get_ps_data('denominate_workflows', {})
        return list(wfls.keys())

    def get_denominate_wfl(self, uuid):
        wfls = self.wallet.db.get_ps_data('denominate_workflows', {})
        wfl = wfls.get(uuid)
        if wfl:
            return PSDenominateWorkflow._from_uuid_and_tuple(uuid, wfl)

    def clear_denominate_wfl(self, uuid):
        self.wallet.db.pop_ps_data('denominate_workflows', uuid)

    def set_denominate_wfl(self, workflow):
        wfl_dict = workflow._as_dict()
        self.wallet.db.update_ps_data('denominate_workflows', wfl_dict)

    @property
    def is_mixing_run(self):
        return self._is_mixing_run

    @property
    def is_mixing_changes(self):
        return self._is_mixing_changes

    def is_mixing_run_data(self, short_txt=None, full_txt=None):
        if short_txt:
            if self.is_mixing_run:
                return _('Stop Mixing')
            else:
                return _('Start Mixing')
        elif full_txt:
            return _('Start/Stop PrivateSend mixing process')

    @property
    def mix_start_time(self):
        return self._mix_start_time

    @property
    def mix_stop_time(self):
        return self._mix_stop_time

    @property
    def after_mixing_stop_wait_time(self):
        '''Wait seconds after mixing stop to spend PS coins in regular tx'''
        return AFTER_MIXING_STOP_WAIT_TIME

    @property
    def double_spend_warn(self):
        if self.is_mixing_run:
            wait_time = self.after_mixing_stop_wait_time
            return _('PrivateSend mixing is currently run. To prevent double'
                     ' spending it is recommended to stop mixing and wait'
                     ' {} seconds before spending PrivateSend'
                     ' coins.'.format(wait_time))
        if self.mix_stop_time is not None:
            stop_secs_ago = round(time.time() - self.mix_stop_time)
            wait_time = self.after_mixing_stop_wait_time
            if stop_secs_ago < wait_time:
                wait_secs = wait_time - stop_secs_ago
                return _('PrivateSend mixing is recently run. To prevent'
                         ' double spending It is recommended to wait'
                         ' {} seconds before spending PrivateSend'
                         ' coins. To bypass limitation restart'
                         ' wallet app.'.format(wait_secs))
        return ''

    @property
    def spend_ps_coins_warn(self):
        return _('It is not recommended to spend PrivateSend'
                 ' coins in regular transactions.')

    def dn_balance_data(self, short_txt=None, full_txt=None):
        if short_txt:
            return _('Denominated Balance')
        elif full_txt:
            return _('Currently available denominated balance')

    def ps_balance_data(self, short_txt=None, full_txt=None):
        if short_txt:
            return _('PrivateSend Balance')
        elif full_txt:
            return _('Currently available anonymized balance')

    def ps_debug_data(self, short_txt=None, full_txt=None):
        if short_txt:
            return _('PrivateSend Debug Info')
        elif full_txt:
            return _('Show PrivateSend Mixing process details')

    def get_ps_data_info(self):
        res = []
        denominate_wfls = []
        with self.denominate_wfl_lock:
            for uuid in self.denominate_wfl_list:
                wfl = self.get_denominate_wfl(uuid)
                denominate_wfls.append(wfl)

        pay_collateral_wfl = None
        with self.pay_collateral_wfl_lock:
            wfl = self.pay_collateral_wfl
            if wfl:
                pay_collateral_wfl = wfl

        new_collateral_wfl = None
        with self.new_collateral_wfl_lock:
            wfl = self.new_collateral_wfl
            if wfl:
                new_collateral_wfl = wfl

        new_denoms_wfl = None
        with self.new_denoms_wfl_lock:
            wfl = self.new_denoms_wfl
            if wfl:
                new_denoms_wfl = wfl

        w = self.wallet
        data = w.db.get_ps_txs()
        res.append(f'PrivateSend transactions count: {len(data)}')
        data = w.db.get_ps_txs_removed()
        res.append(f'Removed PrivateSend transactions count: {len(data)}')

        data = w.db.get_ps_denoms()
        res.append(f'ps_denoms count: {len(data)}')
        data = w.db.get_ps_spent_denoms()
        res.append(f'ps_spent_denoms count: {len(data)}')
        data = w.db.get_ps_spending_denoms()
        res.append(f'ps_spending_denoms count: {len(data)}')

        data = w.db.get_ps_collaterals()
        res.append(f'ps_collaterals count: {len(data)}')
        data = w.db.get_ps_spent_collaterals()
        res.append(f'ps_spent_collaterals count: {len(data)}')
        data = w.db.get_ps_spending_collaterals()
        res.append(f'ps_spending_collaterals count: {len(data)}')

        data = w.db.get_ps_others()
        res.append(f'ps_others count: {len(data)}')
        data = w.db.get_ps_spent_others()
        res.append(f'ps_spent_others count: {len(data)}')

        data = w.db.get_ps_reserved()
        res.append(f'Reserved addresses count: {len(data)}')

        if pay_collateral_wfl:
            res.append(f'Pay collateral workflow data exists')

        if new_collateral_wfl:
            res.append(f'New collateral workflow data exists')

        if new_denoms_wfl:
            res.append(f'New denoms workflow data exists')

        if denominate_wfls:
            cnt = len(denominate_wfls)
            res.append(f'Denominate workflow data exists, count: {cnt}')
        return res

    def mixing_progress(self, count_on_rounds=None):
        w = self.wallet
        dn_balance = sum(w.get_balance(include_ps=False, min_rounds=0))
        if dn_balance == 0:
            return 0
        dn_balance = sum(w.get_balance(include_ps=False, min_rounds=0))
        r = self.mix_rounds if count_on_rounds is None else count_on_rounds
        ps_balance = sum(w.get_balance(include_ps=False, min_rounds=r))
        if dn_balance == ps_balance:
            return 100
        res = 0
        for i in range(1, r+1):
            ri_balance = sum(w.get_balance(include_ps=False, min_rounds=i))
            res += ri_balance/dn_balance/r
        res = round(res*100)
        if res < 100:  # on small amount differences show 100 percents to early
            return res
        else:
            return 99

    def mixing_progress_data(self, short_txt=None, full_txt=None):
        if short_txt:
            return _('Mixing Progress')
        elif full_txt:
            return _('Mixing Progress in percents')

    @property
    def all_mixed(self):
        w = self.wallet
        dn_balance = sum(w.get_balance(include_ps=False, min_rounds=0))
        if dn_balance == 0:
            return False
        r = self.mix_rounds
        ps_balance = sum(w.get_balance(include_ps=False, min_rounds=r))
        return (dn_balance and ps_balance >= dn_balance)

    def cache_keypairs(self, password):
        if self._keypairs_cache:
            self.cleanup_keypairs_cache()
        w = self.wallet

        # add spendable coins keys
        for c in w.get_utxos(None, include_ps=True):
            addr = c['address']
            sequence = w.get_address_index(addr)
            x_pubkey = w.keystore.get_xpubkey(*sequence)
            sec = w.keystore.get_private_key(sequence, password)
            self._keypairs_cache[x_pubkey] = sec

        # add unused coins keys
        with self.reserved_lock:
            first_recv_addr = self.reserve_addresses(1)[0]
            w.db.pop_ps_reserved(first_recv_addr)
            first_change_addr = self.reserve_addresses(1, for_change=True)[0]
            w.db.pop_ps_reserved(first_change_addr)
        first_recv_index = w.get_address_index(first_recv_addr)[1]
        first_change_index = w.get_address_index(first_change_addr)[1]

        for ri in range(first_recv_index,
                        first_recv_index + self.num_keys_to_cache):
            sequence = [0, ri]
            x_pubkey = w.keystore.get_xpubkey(*sequence)
            sec = w.keystore.get_private_key(sequence, password)
            self._keypairs_cache[x_pubkey] = sec

        for ci in range(first_change_index,
                        first_change_index + self.num_keys_to_cache//5 + 1):
            sequence = [1, ci]
            x_pubkey = w.keystore.get_xpubkey(*sequence)
            sec = w.keystore.get_private_key(sequence, password)
            self._keypairs_cache[x_pubkey] = sec

    def cleanup_keypairs_cache(self):
        if not self._keypairs_cache:
            return False
        x_pubkeys = list(self._keypairs_cache.keys())
        for x_pubkey in x_pubkeys:
            del self._keypairs_cache[x_pubkey]
        return True

    def sign_transaction(self, tx, password, mine_txins_cnt=None):
        if self._keypairs_cache:
            if mine_txins_cnt is None:
                tx.add_inputs_info(self.wallet)
            signed_txins_cnt = tx.sign(self._keypairs_cache)
            if mine_txins_cnt is None:
                mine_txins_cnt = len(tx.inputs())
            if signed_txins_cnt < mine_txins_cnt:
                self.logger.debug(f'mine txins cnt: {mine_txins_cnt},'
                                  f' signed txins cnt: {signed_txins_cnt}')
                raise SignWithKeyipairsFailed('Tx signing failed')
        else:
            self.wallet.sign_transaction(tx, password)
        return tx

    def check_protx_info_completeness(self):
        if not self.network:
            return False
        mn_list = self.network.mn_list
        if mn_list.protx_info_completeness < 0.75:
            return False
        else:
            return True

    def check_llmq_ready(self):
        if not self.network:
            return False
        mn_list = self.network.mn_list
        return mn_list.llmq_ready

    def start_mixing(self, password):
        if not self.network:
            self.logger.info('Can not start mixing. Network is not available')
            return
        if self.is_mixing_run or self.is_mixing_changes:
            return
        if self.all_mixed:
            msg = self.ALL_MIXED_MSG
            self.trigger_callback('ps-mixing-changes', self.wallet, msg)
            return
        if not self.check_llmq_ready():
            msg = self.LLMQ_DATA_NOT_READY
            self.trigger_callback('ps-mixing-changes', self.wallet, msg)
            return
        if not self.check_protx_info_completeness():
            msg = self.MNS_DATA_NOT_READY
            self.trigger_callback('ps-mixing-changes', self.wallet, msg)
            return
        if not self.wallet.db.get_ps_txs():  # ps data can be cleared by clear
            try:                             # history or this wallet restored
                self.find_untracked_ps_txs()
            except Exception as e:
                msg = (f'Can not start mixing, find_untracked_ps_txs'
                       f' failed with: {str(e)}')
                self.logger.info(msg)
                self.trigger_callback('ps-mixing-changes', self.wallet, msg)
                return
        err = self.check_ps_txs()
        if err:
            msg = (f'Can not start mixing, check_ps_txs'
                   f' failed with: {err}')
            self.logger.info(msg)
            self.trigger_callback('ps-mixing-changes', self.wallet, msg)
            return
        self._is_mixing_changes = True
        self.trigger_callback('ps-mixing-changes', self.wallet, None)

        fut = asyncio.run_coroutine_threadsafe(self._start_mixing(password),
                                               self.loop)
        try:
            fut.result(timeout=2)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass

        self._is_mixing_run = True
        self._is_mixing_changes = False
        self._mix_start_time = time.time()
        self.trigger_callback('ps-mixing-changes', self.wallet, None)

    async def _start_mixing(self, password):
        if not self.enabled or not self.network:
            return

        if self.wallet.has_keystore_encryption():
            self._wait_to_cache_keypairs = True
        else:
            self.cleanup_keypairs_cache()
            self._wait_to_cache_keypairs = False

        assert not self.main_taskgroup
        self.main_taskgroup = main_taskgroup = SilentTaskGroup()
        self.logger.info('Starting PrivateSend Mixing')

        async def main():
            try:
                async with main_taskgroup as group:
                    await group.spawn(self._make_keypairs_cache(password))
                    await group.spawn(self._check_all_mixed())
                    await group.spawn(self._maintain_pay_collateral_tx())
                    await group.spawn(self._maintain_collateral_amount())
                    await group.spawn(self._maintain_denoms())
                    await group.spawn(self._mix_denoms())
            except Exception as e:
                self.logger.exception('')
                raise e
        asyncio.run_coroutine_threadsafe(main(), self.loop)
        self.logger.info('Started PrivateSend Mixing')

    def stop_mixing_from_async_thread(self, msg):
        self.loop.run_in_executor(None, self.stop_mixing, msg)

    def stop_mixing(self, msg=None):
        if not self.is_mixing_run or self.is_mixing_changes:
            return
        self._is_mixing_changes = True
        self.trigger_callback('ps-mixing-changes', self.wallet, None)

        fut = asyncio.run_coroutine_threadsafe(self._stop_mixing(), self.loop)
        try:
            fut.result(timeout=2)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass

        if self.cleanup_keypairs_cache():
            self.logger.info('Cleaned Keyparis Cache')

        self._mix_stop_time = time.time()
        self._is_mixing_changes = False
        self._is_mixing_run = False
        if msg is not None:
            stop_msg = f'%s\n\n{msg}' % _('PrivateSend mixing is stoppped!')
        else:
            stop_msg = None
        self.trigger_callback('ps-mixing-changes', self.wallet, stop_msg)

    @log_exceptions
    async def _stop_mixing(self):
        if not self.main_taskgroup:
            return
        self.logger.info('Stopping PrivateSend Mixing')
        try:
            await asyncio.wait_for(self.main_taskgroup.cancel_remaining(),
                                   timeout=2)
        except (asyncio.TimeoutError, asyncio.CancelledError) as e:
            self.logger.debug(f'Exception during main_taskgroup cancellation: '
                              f'{repr(e)}')
        self.logger.info('Stopped PrivateSend Mixing')
        self.main_taskgroup = None

    async def _make_keypairs_cache(self, password):
        if self._wait_to_cache_keypairs:
            self.logger.info('Making Keyparis Cache')
            _make_cache = self.cache_keypairs
            await self.loop.run_in_executor(None, _make_cache, password)
            self._wait_to_cache_keypairs = False
            self.logger.info('Keyparis Cache Done')

    async def _check_all_mixed(self):
        while not self.main_taskgroup.closed():
            await asyncio.sleep(10)
            if self.all_mixed:
                self.stop_mixing_from_async_thread(self.ALL_MIXED_MSG)

    async def _maintain_pay_collateral_tx(self):
        while not self.main_taskgroup.closed():
            if self._wait_to_cache_keypairs:
                await asyncio.sleep(5)
                continue
            wfl = self.pay_collateral_wfl
            if wfl:
                if not wfl.completed or not wfl.tx_order:
                    await self.cleanup_pay_collateral_wfl()
            elif self.ps_collateral_cnt == 1:
                if not self.get_confirmed_ps_collateral_data():
                    await asyncio.sleep(5)
                    continue
                await self.prepare_pay_collateral_wfl()
            await asyncio.sleep(0.25)

    async def _maintain_collateral_amount(self):
        while not self.main_taskgroup.closed():
            if self._wait_to_cache_keypairs:
                await asyncio.sleep(5)
                continue
            wfl = self.new_collateral_wfl
            if wfl:
                if not wfl.completed or not wfl.tx_order:
                    await self.cleanup_new_collateral_wfl()
                elif wfl.completed and wfl.next_to_send(self.wallet):
                    await self.broadcast_new_collateral_wfl()
            elif (not self.ps_collateral_cnt
                    and not self.calc_need_denoms_amounts(use_cache=True)):
                await self.create_new_collateral_wfl()
            await asyncio.sleep(0.25)

    async def _maintain_denoms(self):
        while not self.main_taskgroup.closed():
            if self._wait_to_cache_keypairs:
                await asyncio.sleep(5)
                continue
            wfl = self.new_denoms_wfl
            if wfl:
                if not wfl.completed or not wfl.tx_order:
                    await self.cleanup_new_denoms_wfl()
                elif wfl.completed and wfl.next_to_send(self.wallet):
                    await self.broadcast_new_denoms_wfl()
            elif self.calc_need_denoms_amounts(use_cache=True):
                await self.create_new_denoms_wfl()
            await asyncio.sleep(0.25)

    async def _mix_denoms(self):
        def _cleanup():
            for uuid in self.denominate_wfl_list:
                wfl = self.get_denominate_wfl(uuid)
                if not wfl.completed:
                    self._cleanup_denominate_wfl(wfl)
        await self.loop.run_in_executor(None, _cleanup)

        while not self.main_taskgroup.closed():
            if self._wait_to_cache_keypairs:
                await asyncio.sleep(5)
                continue
            num_workflows = len(self.denominate_wfl_list)
            if (self._denoms_to_mix_cache
                    and self.pay_collateral_wfl
                    and num_workflows < self.max_sessions):
                await self.main_taskgroup.spawn(self.start_denominate_wfl())
            await asyncio.sleep(0.25)

    def check_min_rounds(self, coins, min_rounds):
        for c in coins:
            ps_rounds = c['ps_rounds']
            if ps_rounds is None or ps_rounds < min_rounds:
                raise PSMinRoundsCheckFailed(f'Check for mininum {min_rounds}'
                                             f' PrivateSend mixing rounds'
                                             f' failed')

    async def start_mix_session(self, denom_value, dsq):
        n_denom = PS_DENOMS_DICT[denom_value]
        sess = PSMixSession(self, denom_value, n_denom, dsq)
        peer_str = sess.peer_str
        async with self.mix_sessions_lock:
            if peer_str in self.mix_sessions:
                raise Exception(f'Session with {peer_str} already exists')
            await sess.run_peer()
            self.mix_sessions[peer_str] = sess
            return sess

    async def stop_mix_session(self, peer_str):
        async with self.mix_sessions_lock:
            sess = self.mix_sessions.pop(peer_str)
            if not sess:
                self.logger.debug(f'Peer {peer_str} not found in mix_session')
                return
            sess.close_peer()
            return sess

    def get_addresses(self, include_ps=False, min_rounds=None,
                      for_change=None):
        if for_change is None:
            all_addrs = self.wallet.get_addresses()
        elif for_change:
            all_addrs = self.wallet.get_change_addresses()
        else:
            all_addrs = self.wallet.get_receiving_addresses()
        if include_ps:
            return all_addrs
        else:
            ps_addrs = self.wallet.db.get_ps_addresses(min_rounds=min_rounds)
        if min_rounds is not None:
            return [addr for addr in all_addrs if addr in ps_addrs]
        else:
            return [addr for addr in all_addrs if addr not in ps_addrs]

    def reserve_addresses(self, addrs_count=1, for_change=False, data=None):
        assert type(for_change) == bool
        result = []
        w = self.wallet
        with w.lock:
            while len(result) < addrs_count:
                if for_change:
                    unused = w.calc_unused_change_addresses()
                else:
                    unused = w.get_unused_addresses()
                if unused:
                    addr = unused[0]
                else:
                    addr = w.create_new_address(for_change)
                w.db.add_ps_reserved(addr, data)
                result.append(addr)
        return result

    def add_spent_addrs(self, addrs):
        w = self.wallet
        unspent = w.db.get_unspent_ps_addresses()
        for addr in addrs:
            if addr in unspent:
                continue
            self.spent_addrs.add(addr)

    def restore_spent_addrs(self, addrs):
        for addr in addrs:
            self.spent_addrs.remove(addr)
            self.subscribe_spent_addr(addr)

    def subscribe_spent_addr(self, addr):
        w = self.wallet
        if addr in self.unsubscribed_addrs:
            self.unsubscribed_addrs.remove(addr)
            if w.synchronizer:
                self.logger.debug(f'Add {addr} to synchronizer')
                w.synchronizer.add(addr)

    def unsubscribe_spent_addr(self, addr, hist):
        if (self.subscribe_spent
                or addr not in self.spent_addrs
                or addr in self.unsubscribed_addrs
                or not hist):
            return
        w = self.wallet
        local_height = w.get_local_height()
        for hist_item in hist:
            txid = hist_item[0]
            verified_tx = w.db.verified_tx.get(txid)
            if not verified_tx:
                return
            height = verified_tx[0]
            conf = local_height - height + 1
            if conf < 6:
                return
        self.unsubscribed_addrs.add(addr)
        if w.synchronizer:
            self.logger.debug(f'Remove {addr} from synchronizer')
            w.synchronizer.remove_addr(addr)

    def calc_need_denoms_amounts(self, coins=None, use_cache=False):
        if use_cache:
            denoms_amount = self._ps_denoms_amount_cache
        else:
            denoms_amount = sum(self.wallet.get_balance(include_ps=False,
                                                        min_rounds=0))
        if coins is not None:
            coins_amount = 0
            for c in coins:
                coins_amount += c['value']
            max_keep_amount_duffs = to_duffs(MAX_KEEP_AMOUNT)
            if coins_amount + denoms_amount > max_keep_amount_duffs:
                need_amount = max_keep_amount_duffs
            else:
                need_amount = coins_amount
                # room for fees
                need_amount -= PS_DENOMS_VALS[0] + COLLATERAL_VAL + 1
        else:
            keep_amount_duffs = to_duffs(self.keep_amount)
            need_amount = keep_amount_duffs - denoms_amount
            need_amount += COLLATERAL_VAL  # room for fees
        if need_amount < COLLATERAL_VAL:
            return []

        denoms_amounts = []
        denoms_total = 0
        approx_found = False

        while not approx_found:
            cur_approx_amounts = []

            for dval in PS_DENOMS_VALS:
                for dn in range(11):  # max 11 values of same denom
                    if denoms_total + dval > need_amount:
                        if dval == PS_DENOMS_VALS[0]:
                            approx_found = True
                            denoms_total += dval
                            cur_approx_amounts.append(dval)
                        break
                    else:
                        denoms_total += dval
                        cur_approx_amounts.append(dval)
                if approx_found:
                    break

            denoms_amounts.append(cur_approx_amounts)
        return denoms_amounts

    def denoms_to_mix(self, mix_rounds=None, denom_value=None):
        res = {}
        w = self.wallet
        if mix_rounds is not None:
            denoms = w.db.get_ps_denoms(min_rounds=mix_rounds,
                                        max_rounds=mix_rounds)
        else:
            denoms = w.db.get_ps_denoms(max_rounds=self.mix_rounds-1)
        for outpoint, denom in denoms.items():
            if denom_value is not None and denom_value != denom[1]:
                continue
            if not w.db.get_ps_spending_denom(outpoint):
                res.update({outpoint: denom})
        return res

    def sort_outputs(self, tx):
        def sort_denoms_fn(o):
            if o.value == CREATE_COLLATERAL_VAL:
                rank = 0
            elif o.value in PS_DENOMS_VALS:
                rank = 1
            else:
                rank = 2
            return (rank, o.value)
        tx._outputs.sort(key=sort_denoms_fn)

    # Workflow methods for pay collateral transaction
    def get_confirmed_ps_collateral_data(self):
        w = self.wallet
        outpoint, ps_collateral = w.db.get_ps_collateral()
        if not ps_collateral:
            return

        addr, value = ps_collateral
        utxos = w.get_utxos([addr], min_rounds=PSCoinRounds.COLLATERAL,
                            confirmed_only=True, consider_islocks=True)
        inputs = []
        for utxo in utxos:
            prev_h = utxo['prevout_hash']
            prev_n = utxo['prevout_n']
            if f'{prev_h}:{prev_n}' != outpoint:
                continue
            w.add_input_info(utxo)
            inputs.append(utxo)
        if not inputs:
            self.logger.info(f'ps_collateral is not confirmed')
            return
        return outpoint, value, inputs

    async def prepare_pay_collateral_wfl(self):
        try:
            _prepare = self._prepare_pay_collateral_tx
            res = await self.loop.run_in_executor(None, _prepare)
            if res:
                txid, wfl = res
                self.logger.info(f'Completed pay collateral workflow with tx:'
                                 f' {txid}, workflow: {wfl.uuid}')
                self.wallet.storage.write()
        except Exception as e:
            if type(e) == NoDynamicFeeEstimates:
                msg = self.NO_DYNAMIC_FEE_MSG.format(e)
                self.stop_mixing_from_async_thread(msg)
            if type(e) == SignWithKeyipairsFailed:
                msg = self.NOT_ENOUGH_KEYS_MSG
                self.stop_mixing_from_async_thread(msg)
            wfl = self.pay_collateral_wfl
            if wfl:
                self.logger.info(f'Error creating pay collateral tx:'
                                 f' {str(e)}, workflow: {wfl.uuid}')
                await self.cleanup_pay_collateral_wfl(force=True)

    def _prepare_pay_collateral_tx(self):
        with self.pay_collateral_wfl_lock:
            if self.pay_collateral_wfl:
                return

            res = self.get_confirmed_ps_collateral_data()
            if not res:
                return
            outpoint, value, inputs = res

            uuid = str(uuid4())
            wfl = PSTxWorkflow(uuid=uuid)
            self.set_pay_collateral_wfl(wfl)
            self.logger.info(f'Started up pay collateral workflow: {uuid}')

            self.add_ps_spending_collateral(outpoint, wfl.uuid)
            if value >= COLLATERAL_VAL*2:
                ovalue = value - COLLATERAL_VAL
                with self.reserved_lock:
                    chg_addrs = self.reserve_addresses(1, for_change=True,
                                                       data=uuid)
                outputs = [TxOutput(TYPE_ADDRESS, chg_addrs[0], ovalue)]
            else:
                # OP_RETURN as ouptut script
                outputs = [TxOutput(TYPE_SCRIPT, '6a', 0)]

            tx = Transaction.from_io(inputs[:], outputs[:], locktime=0)
            tx.inputs()[0]['sequence'] = 0xffffffff
            tx = self.sign_transaction(tx, None)
            txid = tx.txid()
            raw_tx = tx.serialize_to_network()
            tx_type = PSTxTypes.PAY_COLLATERAL
            wfl.add_tx(txid=txid, raw_tx=raw_tx, tx_type=tx_type)
            wfl.completed = True
            self.set_pay_collateral_wfl(wfl)
            return txid, wfl

    async def cleanup_pay_collateral_wfl(self, force=False):
        _cleanup = self._cleanup_pay_collateral_wfl
        changed = await self.loop.run_in_executor(None, _cleanup, force)
        if changed:
            self.wallet.storage.write()

    def _cleanup_pay_collateral_wfl(self, force=False):
        with self.pay_collateral_wfl_lock:
            wfl = self.pay_collateral_wfl
            if not wfl or wfl.completed and wfl.tx_order and not force:
                return
        w = self.wallet
        if wfl.tx_order:
            for txid in wfl.tx_order[::-1]:  # use reversed tx_order
                if w.db.get_transaction(txid):
                    w.remove_transaction(txid)
                else:
                    self._cleanup_pay_collateral_wfl_tx_data(txid)
        else:
            self._cleanup_pay_collateral_wfl_tx_data()
        return True

    def _cleanup_pay_collateral_wfl_tx_data(self, txid=None):
        with self.pay_collateral_wfl_lock:
            wfl = self.pay_collateral_wfl
            if not wfl:
                return
            if txid:
                tx_data = wfl.pop_tx(txid)
                if tx_data:
                    self.set_pay_collateral_wfl(wfl)
                    self.logger.info(f'Cleaned up pay collateral tx:'
                                     f' {txid}, workflow: {wfl.uuid}')
            if wfl.tx_order:
                return
            w = self.wallet
            with self.collateral_lock:
                cleanup_collaterals_to_spend = []
                spending = w.db.get_ps_spending_collaterals().items()
                for outpoint, uuid in spending:
                    if uuid == wfl.uuid:
                        cleanup_collaterals_to_spend.append(outpoint)
                for outpoint in cleanup_collaterals_to_spend:
                    self.pop_ps_spending_collateral(outpoint)

            with self.reserved_lock:
                for addr in w.db.select_ps_reserved(for_change=True,
                                                    data=wfl.uuid):
                    w.db.pop_ps_reserved(addr)
            self.clear_pay_collateral_wfl()
            self.logger.info(f'Cleaned up pay collateral workflow: {wfl.uuid}')

    def _search_pay_collateral_wfl(self, txid, tx):
        wfl = self.pay_collateral_wfl
        if wfl and wfl.tx_order and txid in wfl.tx_order:
            return wfl

    def _check_on_pay_collateral_wfl(self, txid, tx):
        with self.pay_collateral_wfl_lock:
            wfl = self._search_pay_collateral_wfl(txid, tx)
            if not wfl:
                return False
            err = self._check_pay_collateral_tx_err(txid, tx)
            if err:
                raise AddPSDataError(f'{err}')
            else:
                return True

    def _process_by_pay_collateral_wfl(self, txid, tx):
        w = self.wallet
        with self.pay_collateral_wfl_lock:
            wfl = self._search_pay_collateral_wfl(txid, tx)
            if not wfl:
                return
            wfl.pop_tx(txid)
            self.set_pay_collateral_wfl(wfl)
            self.logger.info(f'Processed tx: {txid} from pay collateral'
                             f' workflow: {wfl.uuid}')
            if not wfl.tx_order:
                with self.collateral_lock:
                    cleanup_collaterals_to_spend = []
                    spending = w.db.get_ps_spending_collaterals().items()
                    for outpoint, uuid in spending:
                        if uuid == wfl.uuid:
                            cleanup_collaterals_to_spend.append(outpoint)
                    for outpoint in cleanup_collaterals_to_spend:
                        self.pop_ps_spending_collateral(outpoint)

                with self.reserved_lock:
                    for addr in w.db.select_ps_reserved(for_change=True,
                                                        data=wfl.uuid):
                        w.db.pop_ps_reserved(addr)
                self.clear_pay_collateral_wfl()
                self.logger.info(f'Finished processing of pay collateral'
                                 f' workflow: {wfl.uuid}')

    def get_pay_collateral_tx(self):
        wfl = self.pay_collateral_wfl
        if not wfl or not wfl.tx_order:
            return
        txid = wfl.tx_order[0]
        tx_data = wfl.tx_data.get(txid)
        if not tx_data:
            return
        return tx_data.raw_tx

    # Workflow methods for new collateral transaction
    def create_new_collateral_wfl_from_gui(self, coins, password):
        if self.is_mixing_run or self.is_mixing_changes:
            return None, ('Can not create new collateral as mixing process'
                          ' is currently run.')
        wfl = self._start_new_collateral_wfl()
        if not wfl:
            return None, ('Can not create new collateral as other new'
                          ' collateral creation process is in progress')
        try:
            txid, tx = self._make_new_collateral_tx(wfl, coins, password)
            if not self.wallet.add_transaction(txid, tx):
                raise Exception(f'Transactiion with txid: {txid}'
                                f' conflicts with current history')
            with self.new_collateral_wfl_lock:
                wfl.completed = True
                self.set_new_collateral_wfl(wfl)
                self.logger.info(f'Completed new collateral workflow'
                                 f' with tx: {txid},'
                                 f' workflow: {wfl.uuid}')
            return wfl, None
        except Exception as e:
            err = str(e)
            self.logger.info(f'Error creating new collateral tx:'
                             f' {err}, workflow: {wfl.uuid}')
            self._cleanup_new_collateral_wfl(force=True)
            self.logger.info(f'Cleaned up new collateral workflow:'
                             f' {wfl.uuid}')
            return None, err

    async def create_new_collateral_wfl(self):
        _start = self._start_new_collateral_wfl
        wfl = await self.loop.run_in_executor(None, _start)
        if not wfl:
            return
        try:
            _make_tx = self._make_new_collateral_tx
            txid, tx = await self.loop.run_in_executor(None, _make_tx, wfl)
            w = self.wallet
            # add_transaction need run in network therad
            if not w.add_transaction(txid, tx):
                raise Exception(f'Transactiion with txid: {txid}'
                                f' conflicts with current history')

            def _after_create_tx():
                with self.new_collateral_wfl_lock:
                    wfl.completed = True
                    self.set_new_collateral_wfl(wfl)
                    self.logger.info(f'Completed new collateral workflow'
                                     f' with tx: {txid},'
                                     f' workflow: {wfl.uuid}')
            await self.loop.run_in_executor(None, _after_create_tx)
            w.storage.write()
        except Exception as e:
            if type(e) == NoDynamicFeeEstimates:
                msg = self.NO_DYNAMIC_FEE_MSG.format(e)
                self.stop_mixing_from_async_thread(msg)
            if type(e) == AddPSDataError:
                msg = self.ADD_PS_DATA_ERR_MSG
                type_name = SPEC_TX_NAMES[PSTxTypes.NEW_COLLATERAL]
                msg = f'{msg} {type_name} {txid}:\n{str(e)}'
                self.stop_mixing_from_async_thread(msg)
            elif type(e) == SignWithKeyipairsFailed:
                msg = self.NOT_ENOUGH_KEYS_MSG
                self.stop_mixing_from_async_thread(msg)
            elif type(e) == NotEnoughFunds:
                msg = _('Insufficient funds to create collateral amount.'
                        ' You can use coin selector to manually create'
                        ' collateral amount from PrivateSend coins.')
                self.stop_mixing_from_async_thread(msg)
            self.logger.info(f'Error creating new collateral tx:'
                             f' {str(e)}, workflow: {wfl.uuid}')
            await self.cleanup_new_collateral_wfl(force=True)

    def _start_new_collateral_wfl(self):
        with self.pay_collateral_wfl_lock, \
                self.new_collateral_wfl_lock, \
                self.new_denoms_wfl_lock:
            if self.new_collateral_wfl:
                return

            uuid = str(uuid4())
            self.set_new_collateral_wfl(PSTxWorkflow(uuid=uuid))
            self.logger.info(f'Started up new collateral workflow: {uuid}')
            return self.new_collateral_wfl

    def _make_new_collateral_tx(self, wfl, coins=None, password=None):
        with self.pay_collateral_wfl_lock, \
                self.new_collateral_wfl_lock, \
                self.new_denoms_wfl_lock:
            if self.pay_collateral_wfl:
                raise Exception('Can not create new collateral as other new'
                                ' collateral amount seems to exists')
            if self.new_denoms_wfl:
                raise Exception('Can not create new collateral as new denoms'
                                ' creation process is in progress')
            if self.config is None:
                raise Exception('self.config is not set')

            w = self.wallet
            old_outpoint, old_collateral = w.db.get_ps_collateral()
            if old_outpoint:
                raise Exception('Can not create new collateral as other new'
                                ' collateral amount exists')

            # try to create new collateral tx with change outupt at first
            uuid = wfl.uuid
            with self.reserved_lock:
                oaddr = self.reserve_addresses(1, data=uuid)[0]
            outputs = [TxOutput(TYPE_ADDRESS, oaddr, CREATE_COLLATERAL_VAL)]
            if coins is None:
                utxos = w.get_utxos(None,
                                    excluded_addresses=w.frozen_addresses,
                                    mature_only=True, confirmed_only=True,
                                    consider_islocks=True)
                utxos = [utxo for utxo in utxos if not w.is_frozen_coin(utxo)]
            else:
                utxos = coins
            tx = w.make_unsigned_transaction(utxos, outputs, self.config)
            # use first input address as a change, use selected inputs
            in0 = tx.inputs()[0]['address']
            tx = w.make_unsigned_transaction(tx.inputs(), outputs,
                                             self.config, change_addr=in0)
            # sort ouptus again (change last)
            self.sort_outputs(tx)
            tx = self.sign_transaction(tx, password)
            txid = tx.txid()
            raw_tx = tx.serialize_to_network()
            tx_type = PSTxTypes.NEW_COLLATERAL
            wfl.add_tx(txid=txid, raw_tx=raw_tx, tx_type=tx_type)
            self.set_new_collateral_wfl(wfl)
            return txid, tx

    async def cleanup_new_collateral_wfl(self, force=False):
        _cleanup = self._cleanup_new_collateral_wfl
        changed = await self.loop.run_in_executor(None, _cleanup, force)
        if changed:
            self.wallet.storage.write()

    def _cleanup_new_collateral_wfl(self, force=False):
        with self.new_collateral_wfl_lock:
            wfl = self.new_collateral_wfl
            if not wfl or wfl.completed and wfl.tx_order and not force:
                return
        w = self.wallet
        if wfl.tx_order:
            for txid in wfl.tx_order[::-1]:  # use reversed tx_order
                if w.db.get_transaction(txid):
                    w.remove_transaction(txid)
                else:
                    self._cleanup_new_collateral_wfl_tx_data(txid)
        else:
            self._cleanup_new_collateral_wfl_tx_data()
        return True

    def _cleanup_new_collateral_wfl_tx_data(self, txid=None):
        with self.new_collateral_wfl_lock:
            wfl = self.new_collateral_wfl
            if not wfl:
                return
            if txid:
                tx_data = wfl.pop_tx(txid)
                if tx_data:
                    self.set_new_collateral_wfl(wfl)
                    self.logger.info(f'Cleaned up new collateral tx:'
                                     f' {txid}, workflow: {wfl.uuid}')
            if wfl.tx_order:
                return
            w = self.wallet
            with self.reserved_lock:
                for addr in w.db.select_ps_reserved(data=wfl.uuid):
                    w.db.pop_ps_reserved(addr)
            self.clear_new_collateral_wfl()
            self.logger.info(f'Cleaned up new collateral workflow: {wfl.uuid}')

    async def broadcast_new_collateral_wfl(self):
        def _check_wfl():
            with self.new_collateral_wfl_lock:
                wfl = self.new_collateral_wfl
                if not wfl:
                    return
                if not wfl.completed:
                    return
            return wfl
        wfl = await self.loop.run_in_executor(None, _check_wfl)
        if not wfl:
            return
        w = self.wallet
        tx_data = wfl.next_to_send(w)
        if not tx_data:
            return
        txid = tx_data.txid
        sent, err = await tx_data.send(self)
        if err:
            def _on_fail():
                with self.new_collateral_wfl_lock:
                    self.set_new_collateral_wfl(wfl)
                self.logger.info(f'Failed broadcast of new collateral tx'
                                 f' {txid}: {err}, workflow {wfl.uuid}')
            await self.loop.run_in_executor(None, _on_fail)
        if sent:
            def _on_success():
                with self.new_collateral_wfl_lock:
                    self.set_new_collateral_wfl(wfl)
                self.logger.info(f'Broadcasted transaction {txid} from new'
                                 f' collateral workflow: {wfl.uuid}')
                tx = Transaction(wfl.tx_data[txid].raw_tx)
                self._process_by_new_collateral_wfl(txid, tx)
                if not wfl.next_to_send(w):
                    self.logger.info(f'Broadcast completed for new collateral'
                                     f' workflow: {wfl.uuid}')
            await self.loop.run_in_executor(None, _on_success)

    def _search_new_collateral_wfl(self, txid, tx):
        wfl = self.new_collateral_wfl
        if wfl and wfl.tx_order and txid in wfl.tx_order:
            return wfl

    def _check_on_new_collateral_wfl(self, txid, tx):
        with self.new_collateral_wfl_lock:
            wfl = self._search_new_collateral_wfl(txid, tx)
            if not wfl:
                return False
            err = self._check_new_collateral_tx_err(txid, tx)
            if err:
                raise AddPSDataError(f'{err}')
            else:
                return True

    def _process_by_new_collateral_wfl(self, txid, tx):
        w = self.wallet
        with self.new_collateral_wfl_lock:
            wfl = self._search_new_collateral_wfl(txid, tx)
            if not wfl:
                return False
            wfl.pop_tx(txid)
            self.set_new_collateral_wfl(wfl)
            self.logger.info(f'Processed tx: {txid} from new collateral'
                             f' workflow: {wfl.uuid}')
            if not wfl.tx_order:
                with self.reserved_lock:
                    for addr in w.db.select_ps_reserved(data=wfl.uuid):
                        w.db.pop_ps_reserved(addr)
                self.clear_new_collateral_wfl()
                self.logger.info(f'Finished processing of new collateral'
                                 f' workflow: {wfl.uuid}')
            return True

    # Workflow methods for new denoms transaction
    def create_new_denoms_wfl_from_gui(self, coins, password):
        if self.is_mixing_run or self.is_mixing_changes:
            return None, ('Can not create new denoms as mixing process'
                          ' is currently run.')
        wfl, outputs_amounts = self._start_new_denoms_wfl(coins)
        if not outputs_amounts:
            return None, ('Can not create new denoms,'
                          ' not enough coins selected')
        if not wfl:
            return None, ('Can not create new denoms as other new'
                          ' denoms creation process is in progress')
        last_tx_idx = len(outputs_amounts) - 1
        w = self.wallet
        for i, tx_amounts in enumerate(outputs_amounts):
            try:
                txid, tx = self._make_new_denoms_tx(wfl, tx_amounts, i,
                                                    coins, password)
                if not w.add_transaction(txid, tx):
                    raise Exception(f'Transactiion with txid: {txid}'
                                    f' conflicts with current history')
                if i == last_tx_idx:
                    with self.new_denoms_wfl_lock:
                        wfl.completed = True
                        self.set_new_denoms_wfl(wfl)
                        self.logger.info(f'Completed new denoms workflow'
                                         f' with tx: {txid},'
                                         f' workflow: {wfl.uuid}')
                    return wfl, None
                else:
                    prev_outputs = tx.outputs()
                    c_prev_outputs = len(prev_outputs)
                    addr = prev_outputs[-1].address
                    utxos = w.get_utxos([addr], min_rounds=PSCoinRounds.OTHER)
                    last_outpoint = f'{txid}:{c_prev_outputs-1}'
                    coins = []
                    for utxo in utxos:
                        prev_h = utxo['prevout_hash']
                        prev_n = utxo['prevout_n']
                        if f'{prev_h}:{prev_n}' != last_outpoint:
                            continue
                        coins.append(utxo)
            except Exception as e:
                err = str(e)
                self.logger.info(f'Error creating new denoms tx:'
                                 f' {err}, workflow: {wfl.uuid}')
                self._cleanup_new_denoms_wfl(force=True)
                self.logger.info(f'Cleaned up new denoms workflow:'
                                 f' {wfl.uuid}')
                return None, err

    async def create_new_denoms_wfl(self):
        _start = self._start_new_denoms_wfl
        wfl, outputs_amounts = await self.loop.run_in_executor(None, _start)
        if not wfl:
            return
        last_tx_idx = len(outputs_amounts) - 1
        for i, tx_amounts in enumerate(outputs_amounts):
            try:
                w = self.wallet

                def _check_enough_funds():
                    total = sum([sum(a) for a in outputs_amounts])
                    total += CREATE_COLLATERAL_VAL*3
                    coins = w.get_utxos(None,
                                        excluded_addresses=w.frozen_addresses,
                                        mature_only=True, confirmed_only=True,
                                        consider_islocks=True)
                    coins_total = sum([c['value'] for c in coins
                                       if not w.is_frozen_coin(c)])
                    if coins_total < total:
                        raise NotEnoughFunds()
                if i == 0:
                    await self.loop.run_in_executor(None, _check_enough_funds)
                _make_tx = self._make_new_denoms_tx
                txid, tx = await self.loop.run_in_executor(None, _make_tx,
                                                           wfl, tx_amounts, i)
                # add_transaction need run in network therad
                if not w.add_transaction(txid, tx):
                    raise Exception(f'Transaction with txid: {txid}'
                                    f' conflicts with current history')

                def _after_create_tx():
                    with self.new_denoms_wfl_lock:
                        self.logger.info(f'Created new denoms tx: {txid},'
                                         f' workflow: {wfl.uuid}')
                        if i == last_tx_idx:
                            wfl.completed = True
                            self.set_new_denoms_wfl(wfl)
                            self.logger.info(f'Completed new denoms'
                                             f' workflow: {wfl.uuid}')
                await self.loop.run_in_executor(None, _after_create_tx)
                w.storage.write()
            except Exception as e:
                if type(e) == NoDynamicFeeEstimates:
                    msg = self.NO_DYNAMIC_FEE_MSG.format(e)
                    self.stop_mixing_from_async_thread(msg)
                if type(e) == AddPSDataError:
                    msg = self.ADD_PS_DATA_ERR_MSG
                    type_name = SPEC_TX_NAMES[PSTxTypes.NEW_DENOMS]
                    msg = f'{msg} {type_name} {txid}:\n{str(e)}'
                    self.stop_mixing_from_async_thread(msg)
                elif type(e) == SignWithKeyipairsFailed:
                    msg = self.NOT_ENOUGH_KEYS_MSG
                    self.stop_mixing_from_async_thread(msg)
                elif type(e) == NotEnoughFunds:
                    msg = _('Insufficient funds to create anonymized amount.'
                            ' You can use PrivateSend settings to change'
                            ' amount of Dash to keep anonymized.')
                    self.stop_mixing_from_async_thread(msg)
                self.logger.info(f'Error creating new denoms tx:'
                                 f' {str(e)}, workflow: {wfl.uuid}')
                await self.cleanup_new_denoms_wfl(force=True)
                break

    def _start_new_denoms_wfl(self, coins=None):
        with self.new_denoms_wfl_lock, \
                self.pay_collateral_wfl_lock, \
                self.new_collateral_wfl_lock:
            if self.new_denoms_wfl:
                return None, None

            outputs_amounts = self.calc_need_denoms_amounts(coins=coins)
            if not outputs_amounts:
                return None, None

            if (not self.pay_collateral_wfl
                    and not self.new_collateral_wfl
                    and not self.ps_collateral_cnt):
                outputs_amounts[0].insert(0, CREATE_COLLATERAL_VAL)
            uuid = str(uuid4())
            wfl = PSTxWorkflow(uuid=uuid)
            self.set_new_denoms_wfl(wfl)
            self.logger.info(f'Started up new denoms workflow: {uuid}')
            return wfl, outputs_amounts

    def _make_new_denoms_tx(self, wfl, tx_amounts, i,
                            coins=None, password=None):
        with self.new_denoms_wfl_lock:
            if self.config is None:
                raise Exception('self.config is not set')

            w = self.wallet
            # try to create new denoms tx with change outupt at first
            use_confirmed = (i == 0)  # for first tx use confirmed coins
            addrs_cnt = len(tx_amounts)
            with self.reserved_lock:
                oaddrs = self.reserve_addresses(addrs_cnt, data=wfl.uuid)
            outputs = [TxOutput(TYPE_ADDRESS, addr, a)
                       for addr, a in zip(oaddrs, tx_amounts)]
            if coins is None:
                utxos = w.get_utxos(None,
                                    excluded_addresses=w.frozen_addresses,
                                    mature_only=True,
                                    confirmed_only=use_confirmed,
                                    consider_islocks=True)
                utxos = [utxo for utxo in utxos if not w.is_frozen_coin(utxo)]
            else:
                utxos = coins
            tx = w.make_unsigned_transaction(utxos, outputs, self.config)
            # use first input address as a change, use selected inputs
            in0 = tx.inputs()[0]['address']
            tx = w.make_unsigned_transaction(tx.inputs(), outputs,
                                             self.config, change_addr=in0)
            # sort ouptus again (change last)
            self.sort_outputs(tx)
            tx = self.sign_transaction(tx, password)
            txid = tx.txid()
            raw_tx = tx.serialize_to_network()
            tx_type = PSTxTypes.NEW_DENOMS
            wfl.add_tx(txid=txid, raw_tx=raw_tx, tx_type=tx_type)
            self.set_new_denoms_wfl(wfl)
            return txid, tx

    async def cleanup_new_denoms_wfl(self, force=False):
        _cleanup = self._cleanup_new_denoms_wfl
        changed = await self.loop.run_in_executor(None, _cleanup, force)
        if changed:
            self.wallet.storage.write()

    def _cleanup_new_denoms_wfl(self, force=False):
        with self.new_denoms_wfl_lock:
            wfl = self.new_denoms_wfl
            if not wfl or wfl.completed and wfl.tx_order and not force:
                return
        w = self.wallet
        if wfl.tx_order:
            for txid in wfl.tx_order[::-1]:  # use reversed tx_order
                if w.db.get_transaction(txid):
                    w.remove_transaction(txid)
                else:
                    self._cleanup_new_denoms_wfl_tx_data(txid)
        else:
            self._cleanup_new_denoms_wfl_tx_data()
        return True

    def _cleanup_new_denoms_wfl_tx_data(self, txid=None):
        with self.new_denoms_wfl_lock:
            wfl = self.new_denoms_wfl
            if not wfl:
                return
            if txid:
                tx_data = wfl.pop_tx(txid)
                if tx_data:
                    self.set_new_denoms_wfl(wfl)
                    self.logger.info(f'Cleaned up new denoms tx:'
                                     f' {txid}, workflow: {wfl.uuid}')
            if wfl.tx_order:
                return
            w = self.wallet
            with self.reserved_lock:
                for addr in w.db.select_ps_reserved(data=wfl.uuid):
                    w.db.pop_ps_reserved(addr)
            self.clear_new_denoms_wfl()
            self.logger.info(f'Cleaned up new denoms workflow: {wfl.uuid}')

    async def broadcast_new_denoms_wfl(self):
        def _check_wfl():
            with self.new_denoms_wfl_lock:
                wfl = self.new_denoms_wfl
                if not wfl:
                    return
                if not wfl.completed:
                    return
            return wfl
        wfl = await self.loop.run_in_executor(None, _check_wfl)
        if not wfl:
            return
        w = self.wallet
        tx_data = wfl.next_to_send(w)
        if not tx_data:
            return
        txid = tx_data.txid
        sent, err = await tx_data.send(self)
        if err:
            def _on_fail():
                with self.new_denoms_wfl_lock:
                    self.set_new_denoms_wfl(wfl)
                self.logger.info(f'Failed broadcast of new denoms tx {txid}:'
                                 f' {err}, workflow {wfl.uuid}')
            await self.loop.run_in_executor(None, _on_fail)
        if sent:
            def _on_success():
                with self.new_denoms_wfl_lock:
                    self.set_new_denoms_wfl(wfl)
                self.logger.info(f'Broadcasted transaction {txid} from new'
                                 f' denoms workflow: {wfl.uuid}')
                tx = Transaction(wfl.tx_data[txid].raw_tx)
                self._process_by_new_denoms_wfl(txid, tx)
                if not wfl.next_to_send(w):
                    self.logger.info(f'Broadcast completed for new denoms'
                                     f' workflow: {wfl.uuid}')
            await self.loop.run_in_executor(None, _on_success)

    def _search_new_denoms_wfl(self, txid, tx):
        wfl = self.new_denoms_wfl
        if wfl and wfl.tx_order and txid in wfl.tx_order:
            return wfl

    def _check_on_new_denoms_wfl(self, txid, tx):
        with self.new_denoms_wfl_lock:
            wfl = self._search_new_denoms_wfl(txid, tx)
            if not wfl:
                return False
            err = self._check_new_denoms_tx_err(txid, tx)
            if err:
                raise AddPSDataError(f'{err}')
            else:
                return True

    def _process_by_new_denoms_wfl(self, txid, tx):
        w = self.wallet
        with self.new_denoms_wfl_lock:
            wfl = self._search_new_denoms_wfl(txid, tx)
            if not wfl:
                return
            wfl.pop_tx(txid)
            self.set_new_denoms_wfl(wfl)
            self.logger.info(f'Processed tx: {txid} from new denoms'
                             f' workflow: {wfl.uuid}')
            if not wfl.tx_order:
                with self.reserved_lock:
                    for addr in w.db.select_ps_reserved(data=wfl.uuid):
                        w.db.pop_ps_reserved(addr)
                self.clear_new_denoms_wfl()
                self.logger.info(f'Finished processing of new denoms'
                                 f' workflow: {wfl.uuid}')
            return

    # Workflow methods for denominate transaction
    async def start_denominate_wfl(self):
        _create_wfl = self._create_denominate_wfl
        wfl = await self.loop.run_in_executor(None, _create_wfl)
        if not wfl:
            return
        try:
            _add_io = self._add_io_to_denominate_wfl
            dsq = None
            session = None
            if random.random() > 0.33:
                self.logger.info(f'Denominate workflow: {wfl.uuid}, try'
                                 f' to get masternode from recent dsq')
                while not self.main_taskgroup.closed():
                    recent_mns = self.recent_mixes_mns
                    dsq = await self.dash_net.get_recent_dsq(recent_mns)
                    self.logger.info(f'Denominate workflow: {wfl.uuid},'
                                     f' get dsq from recent dsq queue'
                                     f' {dsq.masternodeOutPoint}')
                    dval = PS_DENOM_REVERSE_DICT[dsq.nDenom]
                    await self.loop.run_in_executor(None, _add_io, wfl, dval)
                    break
            else:
                self.logger.info(f'Denominate workflow: {wfl.uuid}, try to'
                                 f' create new queue on random masternode')
                await self.loop.run_in_executor(None, _add_io, wfl)
            if not wfl.inputs:
                return

            session = await self.start_mix_session(wfl.denom, dsq)

            pay_collateral_tx = self.get_pay_collateral_tx()
            if not pay_collateral_tx:
                raise Exception('Absent suitable pay collateral tx')

            await session.send_dsa(pay_collateral_tx)
            timeout = PRIVATESEND_DSQ_TIMEOUT
            while True:
                cmd, res = await session.read_next_msg(wfl, timeout)
                if not cmd:
                    raise Exception('peer connection closed')
                elif cmd == 'dssu':
                    continue
                elif cmd == 'dsq':
                    if session.fReady:
                        break

            pay_collateral_tx = self.get_pay_collateral_tx()
            if not pay_collateral_tx:
                raise Exception('Absent suitable pay collateral tx')

            final_tx = None
            await session.send_dsi(wfl.inputs, pay_collateral_tx, wfl.outputs)
            while True:
                cmd, res = await session.read_next_msg(wfl)
                if not cmd:
                    raise Exception('peer connection closed')
                elif cmd == 'dssu':
                    continue
                elif cmd == 'dsf':
                    final_tx = res
                    break

            signed_inputs = self._sign_inputs(final_tx, wfl.inputs)
            # run _set_completed early on incidents when connection lost
            def _set_completed():
                with self.denominate_wfl_lock:
                    saved = self.get_denominate_wfl(wfl.uuid)
                    if saved and saved.uuid == wfl.uuid:
                        saved.completed = True
                        self.set_denominate_wfl(saved)
                        return saved
                    else:
                        self.logger.info(f'denominate workflow:'
                                         f' {wfl.uuid} not found')
                        return wfl
            wfl = await self.loop.run_in_executor(None, _set_completed)
            await session.send_dss(signed_inputs)
            while True:
                cmd, res = await session.read_next_msg(wfl)
                if not cmd:
                    raise Exception('peer connection closed')
                elif cmd == 'dssu':
                    continue
                elif cmd == 'dsc':
                    break

            self.logger.info(f'Completed denominate workflow: {wfl.uuid}')
            self.wallet.storage.write()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            if type(e) == NoDynamicFeeEstimates:
                msg = self.NO_DYNAMIC_FEE_MSG.format(e)
                self.stop_mixing_from_async_thread(msg)
            if type(e) == SignWithKeyipairsFailed:
                msg = self.NOT_ENOUGH_KEYS_MSG
                self.stop_mixing_from_async_thread(msg)
            self.logger.info(f'Error in denominate worfklow:'
                             f' {str(e)}, workflow: {wfl.uuid}')
        finally:
            await self.cleanup_denominate_wfl(wfl)
            if session:
                await self.stop_mix_session(session.peer_str)

    def _create_denominate_wfl(self):
        with self.denominate_wfl_lock:
            if len(self.denominate_wfl_list) >= self.max_sessions:
                return
            if not self._denoms_to_mix_cache:
                return
            uuid = str(uuid4())
            wfl = PSDenominateWorkflow(uuid=uuid)
            self.set_denominate_wfl(wfl)
            self.logger.info(f'Created denominate workflow: {uuid}')
            return wfl

    def _add_io_to_denominate_wfl(self, wfl, denom_value=None):
        with self.denominate_wfl_lock, self.denoms_lock:
            if denom_value is not None:
                denoms = self.denoms_to_mix(denom_value=denom_value)
                wfl.denom = denom_value
            else:
                denoms = self.denoms_to_mix()

            outpoints = list(denoms.keys())
            denom_rounds = None
            txids = []
            icnt = 0
            w = self.wallet
            while icnt < random.randint(1, PRIVATESEND_ENTRY_MAX_SIZE):
                if not outpoints:
                    break
                outpoint = outpoints.pop(random.randint(0, len(outpoints)-1))
                denom = denoms.pop(outpoint)

                if denom_value is None:
                    denom_value = denom[1]
                    wfl.denom = denom_value
                elif denom[1] != denom_value:  # skip other denom values
                    continue

                if denom_rounds is None:
                    denom_rounds = denom[2]
                    wfl.rounds = denom_rounds
                elif denom[2] != denom_rounds:  # skip other denom rounds
                    continue

                txid = outpoint.split(':')[0]
                if txid in txids:  # skip outputs from same tx
                    continue

                height = w.get_tx_height(txid).height
                islock = w.db.get_islock(txid)
                if not islock and height <= 0:  # skip not islocked/confirmed
                    continue

                txids.append(txid)

                wfl.inputs.append(outpoint)
                self.add_ps_spending_denom(outpoint, wfl.uuid)

                with self.reserved_lock:
                    oaddr = self.reserve_addresses(1, data=wfl.uuid)[0]
                wfl.outputs.append(oaddr)

                icnt += 1

            if not wfl.inputs:
                self.logger.info(f'Denominate workflow: {wfl.uuid},'
                                 f' no suitable denoms to mix')
                return

            self.set_denominate_wfl(wfl)
            self.logger.info(f'Denominate workflow: {wfl.uuid} added inputs'
                             f' with denom value {wfl.denom}, denom rounds'
                             f' {wfl.rounds}, count {len(wfl.inputs)}')

    def _sign_inputs(self, tx, inputs):
        signed_inputs = []
        tx = self._sign_denominate_tx(tx)
        for i in tx.inputs():
            prev_h = i['prevout_hash']
            prev_n = i['prevout_n']
            if f'{prev_h}:{prev_n}' not in inputs:
                continue
            prev_h = bfh(prev_h)[::-1]
            prev_n = int(prev_n)
            scriptSig = bfh(i['scriptSig'])
            sequence = i['sequence']
            signed_inputs.append(CTxIn(prev_h, prev_n, scriptSig, sequence))
        return signed_inputs

    def _sign_denominate_tx(self, tx):
        w = self.wallet
        mine_txins_cnt = 0
        for txin in tx.inputs():
            w.add_input_info(txin)
            if txin['address'] is None:
                del txin['num_sig']
                txin['x_pubkeys'] = []
                txin['pubkeys'] = []
                txin['signatures'] = []
                continue
            mine_txins_cnt += 1
        self.sign_transaction(tx, None, mine_txins_cnt)
        raw_tx = tx.serialize()
        return Transaction(raw_tx)

    async def cleanup_denominate_wfl(self, wfl):
        _cleanup = self._cleanup_denominate_wfl
        changed = await self.loop.run_in_executor(None, _cleanup, wfl)
        if changed:
            self.wallet.storage.write()

    def _cleanup_denominate_wfl(self, wfl):
        with self.denominate_wfl_lock:
            if wfl.completed:
                return

            w = self.wallet
            with self.denoms_lock:
                cleanup_denoms_to_spend = []
                for outpoint, uuid in w.db.get_ps_spending_denoms().items():
                    if uuid == wfl.uuid:
                        cleanup_denoms_to_spend.append(outpoint)
                for outpoint in cleanup_denoms_to_spend:
                    self.pop_ps_spending_denom(outpoint)

            with self.reserved_lock:
                reserved = w.db.select_ps_reserved(data=wfl.uuid)
                for addr in reserved:
                    w.db.pop_ps_reserved(addr)

            self.clear_denominate_wfl(wfl.uuid)
            self.logger.info(f'Cleaned up denominate workflow: {wfl.uuid}')
            return True

    def _search_denominate_wfl(self, txid, tx):
        err = self._check_denominate_tx_err(txid, tx, full_check=False)
        if not err:
            for uuid in self.denominate_wfl_list:
                wfl = self.get_denominate_wfl(uuid)
                if not wfl or not wfl.completed:
                    continue
                if self._check_denominate_tx_io_on_wfl(txid, tx, wfl):
                    return wfl

    def _check_on_denominate_wfl(self, txid, tx):
        with self.denominate_wfl_lock:
            wfl = self._search_denominate_wfl(txid, tx)
            err = self._check_denominate_tx_err(txid, tx)
            if not err:
                return True
            if wfl:
                raise AddPSDataError(f'{err}')
            else:
                return False

    def _process_by_denominate_wfl(self, txid, tx):
        w = self.wallet
        with self.denominate_wfl_lock:
            wfl = self._search_denominate_wfl(txid, tx)
            if not wfl:
                return
            with self.denoms_lock:
                cleanup_denoms_to_spend = []
                for outpoint, uuid in w.db.get_ps_spending_denoms().items():
                    if uuid == wfl.uuid:
                        cleanup_denoms_to_spend.append(outpoint)
                for outpoint in cleanup_denoms_to_spend:
                    self.pop_ps_spending_denom(outpoint)

            with self.reserved_lock:
                for addr in w.db.select_ps_reserved(data=wfl.uuid):
                    w.db.pop_ps_reserved(addr)
            self.logger.info(f'Processed tx: {txid} from'
                             f' denominate workflow: {wfl.uuid}')

            self.clear_denominate_wfl(wfl.uuid)
            self.logger.info(f'Finished processing of'
                             f' denominate workflow: {wfl.uuid}')

    def get_workflow_tx_info(self, wfl):
        w = self.wallet
        tx_cnt = len(wfl.tx_order)
        tx_type = None if not tx_cnt else wfl.tx_data[wfl.tx_order[0]].tx_type
        total = 0
        total_fee = 0
        for txid in wfl.tx_order:
            tx = Transaction(wfl.tx_data[txid].raw_tx)
            tx_info = w.get_tx_info(tx)
            total += tx_info.amount
            total_fee += tx_info.fee
        return tx_type, tx_cnt, total, total_fee

    # Methods to check different tx types, add/rm ps data on these types
    def unpack_io_values(func):
        '''Decorator to prepare tx inputs/outputs info'''
        def func_wrapper(self, txid, tx, full_check=True):
            w = self.wallet
            inputs = []
            outputs = []
            icnt = mine_icnt = others_icnt = 0
            ocnt = mine_ocnt = op_return_ocnt = others_ocnt = 0
            for i in tx.inputs():
                icnt += 1
                prev_h = i['prevout_hash']
                prev_n = i['prevout_n']
                prev_tx = w.db.get_transaction(prev_h)
                if prev_tx:
                    o = prev_tx.outputs()[prev_n]
                    if w.is_mine(o.address):
                        inputs.append((o, prev_h, prev_n, True))  # mine
                        mine_icnt += 1
                    else:
                        inputs.append((o, prev_h, prev_n, False))  # others
                        others_icnt += 1
                else:
                    inputs.append((None, prev_h, prev_n, False))  # others
                    others_icnt += 1
            for idx, o in enumerate(tx.outputs()):
                ocnt += 1
                if w.is_mine(o.address):
                    outputs.append((o, txid, idx, True))  # mine
                    mine_ocnt += 1
                elif o.address.lower() == '6a':
                    outputs.append((o, txid, idx, False))  # OP_RETURN
                    op_return_ocnt += 1
                else:
                    outputs.append((o, txid, idx, False))  # others
                    others_ocnt += 1
            io_values = (inputs, outputs,
                         icnt, mine_icnt, others_icnt,
                         ocnt, mine_ocnt, op_return_ocnt, others_ocnt)
            return func(self, txid, io_values, full_check)
        return func_wrapper

    def _add_spend_ps_outpoints_ps_data(self, txid, tx):
        w = self.wallet
        spent_ps_addrs = set()
        for i, txin in enumerate(tx.inputs()):
            spent_prev_h = txin['prevout_hash']
            spent_prev_n = txin['prevout_n']
            spent_outpoint = f'{spent_prev_h}:{spent_prev_n}'

            spent_denom = w.db.get_ps_spent_denom(spent_outpoint)
            if not spent_denom:
                spent_denom = w.db.get_ps_denom(spent_outpoint)
                if spent_denom:
                    w.db.add_ps_spent_denom(spent_outpoint, spent_denom)
                    spent_ps_addrs.add(spent_denom[0])
            uuid = w.db.get_ps_spending_denom(spent_outpoint)
            if uuid:
                with self.denominate_wfl_lock:
                    wfl = self.get_denominate_wfl(uuid)
                    if wfl:  # must cleanup on next mixing run
                        wfl.completed = False
                        self.set_denominate_wfl(wfl)
            self.pop_ps_denom(spent_outpoint)

            spent_collateral = w.db.get_ps_spent_collateral(spent_outpoint)
            if not spent_collateral:
                spent_collateral = w.db.get_ps_collateral(spent_outpoint)
                if spent_collateral:
                    w.db.add_ps_spent_collateral(spent_outpoint,
                                                 spent_collateral)
                    spent_ps_addrs.add(spent_collateral[0])
            uuid = w.db.get_ps_spending_collateral(spent_outpoint)
            if uuid:  # must cleanup on next mixing run
                with self.pay_collateral_wfl_lock:
                    wfl = self.pay_collateral_wfl
                    if wfl:
                        wfl.completed = False
                        self.set_pay_collateral_wfl(wfl)
            w.db.pop_ps_collateral(spent_outpoint)

            spent_other = w.db.get_ps_spent_other(spent_outpoint)
            if not spent_other:
                spent_other = w.db.get_ps_other(spent_outpoint)
                if spent_other:
                    w.db.add_ps_spent_other(spent_outpoint, spent_other)
                    spent_ps_addrs.add(spent_other[0])
            w.db.pop_ps_other(spent_outpoint)
        self.add_spent_addrs(spent_ps_addrs)

    def _rm_spend_ps_outpoints_ps_data(self, txid, tx):
        w = self.wallet
        restored_ps_addrs = set()
        for i, txin in enumerate(tx.inputs()):
            restore_prev_h = txin['prevout_hash']
            restore_prev_n = txin['prevout_n']
            restore_outpoint = f'{restore_prev_h}:{restore_prev_n}'
            tx_type, completed = w.db.get_ps_tx_removed(restore_prev_h)
            if not tx_type:
                restore_denom = w.db.get_ps_denom(restore_outpoint)
                if not restore_denom:
                    restore_denom = \
                        w.db.get_ps_spent_denom(restore_outpoint)
                    if restore_denom:
                        self.add_ps_denom(restore_outpoint, restore_denom)
                        restored_ps_addrs.add(restore_denom[0])
            w.db.pop_ps_spent_denom(restore_outpoint)

            if not tx_type:
                restore_collateral = \
                    w.db.get_ps_collateral(restore_outpoint)
                if not restore_collateral:
                    restore_collateral = \
                        w.db.get_ps_spent_collateral(restore_outpoint)
                    if restore_collateral:
                        w.db.add_ps_collateral(restore_outpoint,
                                               restore_collateral)
                        restored_ps_addrs.add(restore_collateral[0])
            w.db.pop_ps_spent_collateral(restore_outpoint)

            if not tx_type:
                restore_other = w.db.get_ps_other(restore_outpoint)
                if not restore_other:
                    restore_other = \
                        w.db.get_ps_spent_other(restore_outpoint)
                    if restore_other:
                        w.db.add_ps_other(restore_outpoint, restore_other)
                        restored_ps_addrs.add(restore_other[0])
            w.db.pop_ps_spent_other(restore_outpoint)
        self.restore_spent_addrs(restored_ps_addrs)

    @unpack_io_values
    def _check_new_denoms_tx_err(self, txid, io_values, full_check):
        (inputs, outputs,
         icnt, mine_icnt, others_icnt,
         ocnt, mine_ocnt, op_return_ocnt, others_ocnt) = io_values
        if others_icnt > 0:
            return 'Transaction has not mine inputs'
        if op_return_ocnt > 0:
            return 'Transaction has OP_RETURN outputs'
        if others_ocnt > 0:
            return 'Transaction has not mine outputs'
        if mine_icnt == 0:
            return 'Transaction has not enough inputs count'
        if mine_ocnt == 0:
            return 'Transaction has not enough outputs count'

        with self.denoms_lock, self.collateral_lock, self.others_lock:
            w = self.wallet
            o_last, o_prev_h, o_prev_n, o_is_mine = outputs[-1]
            i_first, i_prev_h, i_prev_n, is_mine = inputs[0]
            if o_last.address == i_first.address:  # seems it is change value
                denoms_outputs = outputs[:-1]
            elif o_last.value in PS_DENOMS_VALS:  # maybe no change happens
                denoms_outputs = outputs
            else:
                return f'Unsuitable last output value={o_last.value}'
            dval_cnt = 0
            collateral_count = 0
            denoms_cnt = 0
            last_denom_val = PS_DENOMS_VALS[0]  # must start with minimal denom
            for o, prev_h, prev_n, is_mine in denoms_outputs:
                val = o.value
                addr = o.address
                if w.is_change(addr):
                    return 'Transaction has a change address as output'
                if val not in PS_DENOMS_VALS:
                    if collateral_count > 0:  # one collateral already found
                        return f'Unsuitable output value={val}'
                    if val == CREATE_COLLATERAL_VAL:
                        if self.ps_collateral_cnt:
                            return 'Collateral amount already created'
                        collateral_count += 1
                    continue
                elif val < last_denom_val:  # must increase or be the same
                    return (f'Unsuitable denom value={val}, must be'
                            f' {last_denom_val} or greater')
                elif val == last_denom_val:
                    dval_cnt += 1
                    if dval_cnt > 11:  # max 11 times of same denom val
                        return f'To many denoms of value={val}'
                else:
                    dval_cnt = 1
                    last_denom_val = val
                denoms_cnt += 1
            if denoms_cnt == 0:
                return 'Transaction has no denoms'

    def _add_new_denoms_ps_data(self, txid, tx):
        w = self.wallet
        outputs = tx.outputs()
        last_ouput_idx = len(outputs) - 1
        with self.denoms_lock, self.collateral_lock, self.others_lock:
            self._add_spend_ps_outpoints_ps_data(txid, tx)
            for i, o in enumerate(outputs):
                val = o.value
                if i == last_ouput_idx and val not in PS_DENOMS_VALS:  # change
                    continue
                new_outpoint = f'{txid}:{i}'
                if i == 0 and val == CREATE_COLLATERAL_VAL:  # collaterral
                    new_collateral = (o.address, val)
                    w.db.add_ps_collateral(new_outpoint, new_collateral)
                elif val in PS_DENOMS_VALS:  # denom round 0
                    denom = (o.address, val, 0)
                    self.add_ps_denom(new_outpoint, denom)

    def _rm_new_denoms_ps_data(self, txid, tx):
        w = self.wallet
        outputs = tx.outputs()
        last_ouput_idx = len(outputs) - 1
        with self.denoms_lock, self.collateral_lock, self.others_lock:
            self._rm_spend_ps_outpoints_ps_data(txid, tx)
            for i, o in enumerate(outputs):
                val = o.value
                if i == last_ouput_idx and val not in PS_DENOMS_VALS:  # change
                    continue
                rm_outpoint = f'{txid}:{i}'
                if i == 0 and val == CREATE_COLLATERAL_VAL:  # collaterral
                    w.db.pop_ps_collateral(rm_outpoint)
                elif val in PS_DENOMS_VALS:  # denom
                    self.pop_ps_denom(rm_outpoint)

    @unpack_io_values
    def _check_new_collateral_tx_err(self, txid, io_values, full_check):
        (inputs, outputs,
         icnt, mine_icnt, others_icnt,
         ocnt, mine_ocnt, op_return_ocnt, others_ocnt) = io_values
        if others_icnt > 0:
            return 'Transaction has not mine inputs'
        if op_return_ocnt > 0:
            return 'Transaction has OP_RETURN outputs'
        if others_ocnt > 0:
            return 'Transaction has not mine outputs'
        if mine_icnt == 0:
            return 'Transaction has not enough inputs count'

        with self.denoms_lock, self.collateral_lock, self.others_lock:
            if self.ps_collateral_cnt:
                return 'Collateral amount already created'
            w = self.wallet
            o_last, o_prev_h, o_prev_n, o_is_mine = outputs[-1]
            i_first, i_prev_h, i_prev_n, is_mine = inputs[0]
            if o_last.address == i_first.address:  # seems it is change output
                if mine_ocnt != 2:
                    return 'Transaction has wrong outputs count'
            elif o_last.value == CREATE_COLLATERAL_VAL:  # maybe no change
                if mine_ocnt != 1:
                    return 'Transaction has wrong outputs count'
            else:
                return 'Transaction has wrong outputs count/value'
            o, prev_h, prev_n, o_is_mine = outputs[0]
            if w.is_change(o.address):
                return 'Transaction has a change address as output'

    def _add_new_collateral_ps_data(self, txid, tx):
        w = self.wallet
        outputs = tx.outputs()
        last_ouput_idx = len(outputs) - 1
        with self.denoms_lock, self.collateral_lock, self.others_lock:
            self._add_spend_ps_outpoints_ps_data(txid, tx)
            for i, o in enumerate(outputs):
                val = o.value
                if i == last_ouput_idx and val not in PS_DENOMS_VALS:  # change
                    continue
                new_outpoint = f'{txid}:{i}'
                if i == 0 and val == CREATE_COLLATERAL_VAL:  # collaterral
                    new_collateral = (o.address, val)
                    w.db.add_ps_collateral(new_outpoint, new_collateral)

    def _rm_new_collateral_ps_data(self, txid, tx):
        w = self.wallet
        outputs = tx.outputs()
        last_ouput_idx = len(outputs) - 1
        with self.denoms_lock, self.collateral_lock, self.others_lock:
            self._rm_spend_ps_outpoints_ps_data(txid, tx)
            for i, o in enumerate(outputs):
                val = o.value
                if i == last_ouput_idx and val not in PS_DENOMS_VALS:  # change
                    continue
                rm_outpoint = f'{txid}:{i}'
                if i == 0 and val == CREATE_COLLATERAL_VAL:  # collaterral
                    w.db.pop_ps_collateral(rm_outpoint)

    @unpack_io_values
    def _check_pay_collateral_tx_err(self, txid, io_values, full_check):
        (inputs, outputs,
         icnt, mine_icnt, others_icnt,
         ocnt, mine_ocnt, op_return_ocnt, others_ocnt) = io_values
        if others_icnt > 0:
            return 'Transaction has not mine inputs'
        if op_return_ocnt > 1:
            return 'Transaction has to many OP_RETURN outputs'
        if others_ocnt > 0:
            return 'Transaction has not mine outputs'
        if mine_icnt != 1:
            return 'Transaction has wrong inputs count'
        if (op_return_ocnt == 1 and mine_ocnt != 0
                or op_return_ocnt == 0 and mine_ocnt != 1):
            return 'Transaction has wrong outputs count'

        with self.collateral_lock:
            w = self.wallet
            if not self.ps_collateral_cnt:
                return 'Collateral amount not ready'
            old_outpoint, old_collateral = w.db.get_ps_collateral()
            if not old_outpoint:
                return 'Collateral amount not ready'

            i, i_prev_h, i_prev_n, is_mine = inputs[0]
            if old_outpoint != f'{i_prev_h}:{i_prev_n}':
                return 'Wrong collateral amount outpoint'
            if i.value not in [COLLATERAL_VAL*4,  COLLATERAL_VAL*3,
                               COLLATERAL_VAL*2, COLLATERAL_VAL]:
                return 'Wrong collateral amount'
            o, o_prev_h, o_prev_n, is_mine = outputs[0]
            if o.address.lower() == '6a':
                if o.value != 0:
                    return 'Wrong output collateral amount'
            else:
                if o.value not in [COLLATERAL_VAL*3,  COLLATERAL_VAL*2,
                                   COLLATERAL_VAL]:
                    return 'Wrong output collateral amount'
                if not w.is_change(o.address):
                    return 'Transaction has not change address as output'

    def _add_pay_collateral_ps_data(self, txid, tx):
        w = self.wallet
        with self.collateral_lock:
            in0 = tx.inputs()[0]
            spent_prev_h = in0['prevout_hash']
            spent_prev_n = in0['prevout_n']
            spent_outpoint = f'{spent_prev_h}:{spent_prev_n}'
            spent_collateral = w.db.get_ps_spent_collateral(spent_outpoint)
            spent_ps_addrs = set()
            if not spent_collateral:
                spent_collateral = w.db.get_ps_collateral(spent_outpoint)
                if not spent_collateral:
                    raise AddPSDataError(f'ps_collateral {spent_outpoint}'
                                         f' not found')
                w.db.add_ps_spent_collateral(spent_outpoint, spent_collateral)
                spent_ps_addrs.add(spent_collateral[0])
            w.db.pop_ps_collateral(spent_outpoint)
            self.add_spent_addrs(spent_ps_addrs)

            out0 = tx.outputs()[0]
            addr = out0.address
            if addr.lower() != '6a':
                new_outpoint = f'{txid}:{0}'
                new_collateral = (addr, out0.value)
                w.db.add_ps_collateral(new_outpoint, new_collateral)

    def _rm_pay_collateral_ps_data(self, txid, tx):
        w = self.wallet
        with self.collateral_lock:
            in0 = tx.inputs()[0]
            restore_prev_h = in0['prevout_hash']
            restore_prev_n = in0['prevout_n']
            restore_outpoint = f'{restore_prev_h}:{restore_prev_n}'
            restored_ps_addrs = set()
            tx_type, completed = w.db.get_ps_tx_removed(restore_prev_h)
            if not tx_type:
                restore_collateral = w.db.get_ps_collateral(restore_outpoint)
                if not restore_collateral:
                    restore_collateral = \
                        w.db.get_ps_spent_collateral(restore_outpoint)
                    if not restore_collateral:
                        raise RmPSDataError(f'ps_spent_collateral'
                                            f' {restore_outpoint} not found')
                    w.db.add_ps_collateral(restore_outpoint,
                                           restore_collateral)
                    restored_ps_addrs.add(restore_collateral[0])
            w.db.pop_ps_spent_collateral(restore_outpoint)
            self.restore_spent_addrs(restored_ps_addrs)

            out0 = tx.outputs()[0]
            addr = out0.address
            if addr.lower() != '6a':
                rm_outpoint = f'{txid}:{0}'
                w.db.pop_ps_collateral(rm_outpoint)

    @unpack_io_values
    def _check_denominate_tx_err(self, txid, io_values, full_check):
        (inputs, outputs,
         icnt, mine_icnt, others_icnt,
         ocnt, mine_ocnt, op_return_ocnt, others_ocnt) = io_values
        if icnt != ocnt:
            return 'Transaction has different count of inputs/outputs'
        if icnt < POOL_MIN_PARTICIPANTS:
            return 'Transaction has too small count of inputs/outputs'
        if mine_icnt < 1:
            return 'Transaction has too small count of mine inputs'
        if mine_ocnt != mine_icnt:
            return 'Transaction has wrong count of mine outputs'
        if op_return_ocnt > 0:
            return 'Transaction has OP_RETURN outputs'
        if not full_check:
            return

        out0, o_prev_h, o_prev_n, o_is_mine = outputs[0]
        denom_val = out0.value
        if denom_val not in PS_DENOMS_VALS:
            return f'Unsuitable output value={denom_val}'
        with self.denoms_lock:
            w = self.wallet
            for i, prev_h, prev_n, is_mine, in inputs:
                if not is_mine:
                    continue
                if i.value != denom_val:
                    return f'Unsuitable input value={i.value}'
                denom = w.db.get_ps_denom(f'{prev_h}:{prev_n}')
                if not denom:
                    return f'Transaction input not found in ps_denoms'
            for o, prev_h, prev_n, is_mine in outputs:
                if o.value != denom_val:
                    return f'Unsuitable output value={i.value}'
                if is_mine and w.is_change(o.address):
                    return 'Transaction has a change address as output'

    def _check_denominate_tx_io_on_wfl(self, txid, tx, wfl):
        w = self.wallet
        for i, txin in enumerate(tx.inputs()):
            txin = copy.deepcopy(txin)
            w.add_input_info(txin)
            addr = txin['address']
            if not w.is_mine(addr):
                continue
            prev_h = txin['prevout_hash']
            prev_n = txin['prevout_n']
            outpoint = f'{prev_h}:{prev_n}'
            if outpoint not in wfl.inputs:
                return False
        for i, o in enumerate(tx.outputs()):
            if not w.is_mine(o.address):
                continue
            if o.address not in wfl.outputs:
                return False
            if o.value != wfl.denom:
                return False
        return True

    def _add_denominate_ps_data(self, txid, tx):
        w = self.wallet
        min_inputs_round = 1e9
        with self.denoms_lock:
            spent_ps_addrs = set()
            for i, txin in enumerate(tx.inputs()):
                txin = copy.deepcopy(txin)
                w.add_input_info(txin)
                addr = txin['address']
                if not w.is_mine(addr):
                    continue
                spent_prev_h = txin['prevout_hash']
                spent_prev_n = txin['prevout_n']
                spent_outpoint = f'{spent_prev_h}:{spent_prev_n}'
                spent_denom = w.db.get_ps_spent_denom(spent_outpoint)
                if not spent_denom:
                    spent_denom = w.db.get_ps_denom(spent_outpoint)
                    if not spent_denom:
                        raise AddPSDataError(f'ps_denom {spent_outpoint}'
                                             f' not found')
                    w.db.add_ps_spent_denom(spent_outpoint, spent_denom)
                    spent_ps_addrs.add(spent_denom[0])
                self.pop_ps_denom(spent_outpoint)
                min_inputs_round = min(min_inputs_round, spent_denom[2])
            self.add_spent_addrs(spent_ps_addrs)

            output_round = min_inputs_round + 1
            for i, o in enumerate(tx.outputs()):
                addr = o.address
                if not w.is_mine(addr):
                    continue
                new_outpoint = f'{txid}:{i}'
                new_denom = (addr, o.value, output_round)
                self.add_ps_denom(new_outpoint, new_denom)

    def _rm_denominate_ps_data(self, txid, tx):
        w = self.wallet
        with self.denoms_lock:
            restored_ps_addrs = set()
            for i, txin in enumerate(tx.inputs()):
                txin = copy.deepcopy(txin)
                w.add_input_info(txin)
                addr = txin['address']
                if not w.is_mine(addr):
                    continue
                restore_prev_h = txin['prevout_hash']
                restore_prev_n = txin['prevout_n']
                restore_outpoint = f'{restore_prev_h}:{restore_prev_n}'
                tx_type, completed = w.db.get_ps_tx_removed(restore_prev_h)
                if not tx_type:
                    restore_denom = w.db.get_ps_denom(restore_outpoint)
                    if not restore_denom:
                        restore_denom = \
                            w.db.get_ps_spent_denom(restore_outpoint)
                        if not restore_denom:
                            raise RmPSDataError(f'ps_denom {restore_outpoint}'
                                                f' not found')
                        self.add_ps_denom(restore_outpoint, restore_denom)
                        restored_ps_addrs.add(restore_denom[0])
                w.db.pop_ps_spent_denom(restore_outpoint)
            self.restore_spent_addrs(restored_ps_addrs)

            for i, o in enumerate(tx.outputs()):
                addr = o.address
                if not w.is_mine(addr):
                    continue
                rm_outpoint = f'{txid}:{i}'
                self.pop_ps_denom(rm_outpoint)

    @unpack_io_values
    def _check_other_ps_coins_tx_err(self, txid, io_values, full_check):
        (inputs, outputs,
         icnt, mine_icnt, others_icnt,
         ocnt, mine_ocnt, op_return_ocnt, others_ocnt) = io_values
        if mine_ocnt == 0:
            return 'Transaction has not mine outputs'

        with self.denoms_lock, self.collateral_lock, self.others_lock:
            w = self.wallet
            for o, prev_h, prev_n, is_mine in outputs:
                addr = o.address
                if not w.is_mine(addr):
                    continue
                if addr in w.db.get_ps_addresses():
                    return
        return 'Transaction has no outputs with ps denoms/collateral addresses'

    @unpack_io_values
    def _check_privatesend_tx_err(self, txid, io_values, full_check):
        (inputs, outputs,
         icnt, mine_icnt, others_icnt,
         ocnt, mine_ocnt, op_return_ocnt, others_ocnt) = io_values
        if others_icnt > 0:
            return 'Transaction has not mine inputs'
        if mine_icnt < 1:
            return 'Transaction has too small count of mine inputs'
        if op_return_ocnt > 0:
            return 'Transaction has OP_RETURN outputs'
        if mine_ocnt + others_ocnt != 1:
            return 'Transaction has wrong count of outputs'

        with self.denoms_lock, self.others_lock:
            w = self.wallet
            for i, prev_h, prev_n, is_mine in inputs:
                if i.value not in PS_DENOMS_VALS:
                    return f'Unsuitable input value={i.value}'
                denom = w.db.get_ps_denom(f'{prev_h}:{prev_n}')
                if not denom:
                    return f'Transaction input not found in ps_denoms'
                if denom[2] < MIN_MIX_ROUNDS:
                    return f'Transaction input mix_rounds too small'

    @unpack_io_values
    def _check_spend_ps_coins_tx_err(self, txid, io_values, full_check):
        (inputs, outputs,
         icnt, mine_icnt, others_icnt,
         ocnt, mine_ocnt, op_return_ocnt, others_ocnt) = io_values
        if others_icnt > 0:
            return 'Transaction has not mine inputs'
        if mine_icnt == 0:
            return 'Transaction has not enough inputs count'

        with self.denoms_lock, self.collateral_lock, self.others_lock:
            w = self.wallet
            for i, prev_h, prev_n, is_mine in inputs:
                spent_outpoint = f'{prev_h}:{prev_n}'
                if w.db.get_ps_denom(spent_outpoint):
                    return
                if w.db.get_ps_collateral(spent_outpoint):
                    return
                if w.db.get_ps_other(spent_outpoint):
                    return
        return 'Transaction has no inputs from ps denoms/collaterals/others'

    def _add_spend_ps_coins_ps_data(self, txid, tx):
        w = self.wallet
        with self.denoms_lock, self.collateral_lock, self.others_lock:
            self._add_spend_ps_outpoints_ps_data(txid, tx)
            for i, o in enumerate(tx.outputs()):  # check to add ps_others
                addr = o.address
                if not w.is_mine(addr):
                    continue
                if addr in w.db.get_ps_addresses():
                    new_outpoint = f'{txid}:{i}'
                    new_other = (addr, o.value)
                    w.db.add_ps_other(new_outpoint, new_other)

    def _rm_spend_ps_coins_ps_data(self, txid, tx):
        w = self.wallet
        with self.denoms_lock, self.collateral_lock, self.others_lock:
            self._rm_spend_ps_outpoints_ps_data(txid, tx)
            for i, o in enumerate(tx.outputs()):  # check to rm ps_others
                addr = o.address
                if not w.is_mine(addr):
                    continue
                if addr in w.db.get_ps_addresses():
                    rm_outpoint = f'{txid}:{i}'
                    w.db.pop_ps_other(rm_outpoint)

    # Methods to add ps data, using preceding methods for different tx types
    def _check_ps_tx_type(self, txid, tx, check_untracked=False):
        if check_untracked:
            if not self._check_new_denoms_tx_err(txid, tx):
                return PSTxTypes.NEW_DENOMS
            if not self._check_new_collateral_tx_err(txid, tx):
                return PSTxTypes.NEW_COLLATERAL
            if not self._check_pay_collateral_tx_err(txid, tx):
                return PSTxTypes.PAY_COLLATERAL
            if not self._check_denominate_tx_err(txid, tx):
                return PSTxTypes.DENOMINATE
        else:
            if self._check_on_denominate_wfl(txid, tx):
                return PSTxTypes.DENOMINATE
            if self._check_on_pay_collateral_wfl(txid, tx):
                return PSTxTypes.PAY_COLLATERAL
            if self._check_on_new_collateral_wfl(txid, tx):
                return PSTxTypes.NEW_COLLATERAL
            if self._check_on_new_denoms_wfl(txid, tx):
                return PSTxTypes.NEW_DENOMS
        if not self._check_other_ps_coins_tx_err(txid, tx):
            return PSTxTypes.OTHER_PS_COINS
        if not self._check_privatesend_tx_err(txid, tx):
            return PSTxTypes.PRIVATESEND
        if not self._check_spend_ps_coins_tx_err(txid, tx):
            return PSTxTypes.SPEND_PS_COINS
        return STANDARD_TX

    def _add_ps_data(self, txid, tx, tx_type):
        w = self.wallet
        w.db.add_ps_tx(txid, tx_type, completed=False)
        if tx_type == PSTxTypes.NEW_DENOMS:
            self._add_new_denoms_ps_data(txid, tx)
        elif tx_type == PSTxTypes.NEW_COLLATERAL:
            self._add_new_collateral_ps_data(txid, tx)
        elif tx_type == PSTxTypes.PAY_COLLATERAL:
            self._add_pay_collateral_ps_data(txid, tx)
        elif tx_type == PSTxTypes.DENOMINATE:
            self._add_denominate_ps_data(txid, tx)
        elif tx_type == PSTxTypes.PRIVATESEND:
            self._add_spend_ps_coins_ps_data(txid, tx)
        elif tx_type == PSTxTypes.SPEND_PS_COINS:
            self._add_spend_ps_coins_ps_data(txid, tx)
        elif tx_type == PSTxTypes.OTHER_PS_COINS:
            self._add_spend_ps_coins_ps_data(txid, tx)
        else:
            raise AddPSDataError(f'{txid} unknow type {tx_type}')
        w.db.pop_ps_tx_removed(txid)
        w.db.add_ps_tx(txid, tx_type, completed=True)

    def _add_tx_ps_data(self, txid, tx):
        '''Used from AddressSynchronizer.add_transaction'''
        w = self.wallet
        tx_type, completed = w.db.get_ps_tx(txid)
        if tx_type and completed:  # ps data already exists
            return

        if not tx_type:  # try to find type in removed ps txs
            tx_type, completed = w.db.get_ps_tx_removed(txid)
            if tx_type:
                self.logger.info(f'_add_tx_ps_data: matched removed tx {txid}')
        if not tx_type:  # check possible types from workflows
            tx_type = self._check_ps_tx_type(txid, tx)
        if not tx_type:
            return
        if tx_type in PS_SAVED_TX_TYPES:
            try:
                type_name = SPEC_TX_NAMES[tx_type]
                self._add_ps_data(txid, tx, tx_type)
                self.trigger_callback('ps-data-updated', w)
                self.logger.debug(f'Added {type_name} {txid} ps data')
            except Exception as e:
                self.logger.info(f'_add_ps_data {txid} failed: {str(e)}')
                if tx_type in [PSTxTypes.NEW_COLLATERAL, PSTxTypes.NEW_DENOMS]:
                    # this two tx tpes added during wfl creation process
                    raise
                if tx_type in [PSTxTypes.PAY_COLLATERAL, PSTxTypes.DENOMINATE]:
                    # this two tx tpes added from network
                    msg = self.ADD_PS_DATA_ERR_MSG
                    msg = f'{msg} {type_name} {txid}:\n{str(e)}'
                    self.stop_mixing_from_async_thread(msg)
            finally:
                if tx_type == PSTxTypes.PAY_COLLATERAL:
                    self._process_by_pay_collateral_wfl(txid, tx)
                elif tx_type == PSTxTypes.DENOMINATE:
                    self._process_by_denominate_wfl(txid, tx)
        else:
            self.logger.info(f'_add_tx_ps_data: {txid} unknonw type {tx_type}')

    # Methods to rm ps data, using preceding methods for different tx types
    def _rm_ps_data(self, txid, tx, tx_type):
        w = self.wallet
        w.db.add_ps_tx_removed(txid, tx_type, completed=False)
        if tx_type == PSTxTypes.NEW_DENOMS:
            self._rm_new_denoms_ps_data(txid, tx)
            self._cleanup_new_denoms_wfl_tx_data(txid)
        elif tx_type == PSTxTypes.NEW_COLLATERAL:
            self._rm_new_collateral_ps_data(txid, tx)
            self._cleanup_new_collateral_wfl_tx_data(txid)
        elif tx_type == PSTxTypes.PAY_COLLATERAL:
            self._rm_pay_collateral_ps_data(txid, tx)
            self._cleanup_pay_collateral_wfl_tx_data(txid)
        elif tx_type == PSTxTypes.DENOMINATE:
            self._rm_denominate_ps_data(txid, tx)
        elif tx_type == PSTxTypes.PRIVATESEND:
            self._rm_spend_ps_coins_ps_data(txid, tx)
        elif tx_type == PSTxTypes.SPEND_PS_COINS:
            self._rm_spend_ps_coins_ps_data(txid, tx)
        elif tx_type == PSTxTypes.OTHER_PS_COINS:
            self._rm_spend_ps_coins_ps_data(txid, tx)
        else:
            raise RmPSDataError(f'{txid} unknow type {tx_type}')
        w.db.pop_ps_tx(txid)
        w.db.add_ps_tx_removed(txid, tx_type, completed=True)

    def _rm_tx_ps_data(self, txid):
        '''Used from AddressSynchronizer.remove_transaction'''
        w = self.wallet
        tx = w.db.get_transaction(txid)
        if not tx:
            self.logger.info(f'_rm_tx_ps_data: {txid} not found')
            return

        tx_type, completed = w.db.get_ps_tx(txid)
        if not tx_type:
            return
        if tx_type in PS_SAVED_TX_TYPES:
            try:
                self._rm_ps_data(txid, tx, tx_type)
                self.trigger_callback('ps-data-updated', w)
            except Exception as e:
                self.logger.info(f'_rm_ps_data {txid} failed: {str(e)}')
        else:
            self.logger.info(f'_rm_tx_ps_data: {txid} unknonw type {tx_type}')

    # Auxiliary methods
    def clear_ps_data(self):
        if self.is_mixing_run:
            msg = _('To clear PrivateSend data stop PrivateSend mixing')
            self.trigger_callback('ps-mixing-changes', self.wallet, msg)
            return
        w = self.wallet
        self.logger.info(f'Clearing PrivateSend wallet data')
        w.db.clear_ps_data()
        self.logger.info(f'All PrivateSend wallet data cleared')
        w.storage.write()
        self.trigger_callback('ps-data-updated', w)

    def find_untracked_ps_txs(self, log=True):
        if self.is_mixing_run:
            msg = _('To find untracked PrivateSend transactions'
                    ' stop PrivateSend mixing')
            self.trigger_callback('ps-mixing-changes', self.wallet, msg)
            return
        found = 0
        w = self.wallet
        self.logger.info(f'Finding untracked PrivateSend transactions')
        with w.lock, w.transaction_lock:
            tx_islocks = {}
            for addr in set(w.get_addresses()):
                for txid, height, islock in w.get_address_history(addr):
                    if txid not in tx_islocks:
                        tx_islocks[txid] = islock
            history = []
            for txid, islock in tx_islocks.items():
                tx = w.db.get_transaction(txid)
                tx_type, completed = w.db.get_ps_tx(txid)
                history.append((txid, tx, tx_type, islock))
            history.sort(key=lambda x: w.get_txpos(x[0], x[3]))
        for txid, tx, tx_type, islock in history:
            if tx_type:  # already processed
                continue
            tx_type = self._check_ps_tx_type(txid, tx, check_untracked=True)
            if tx_type:
                self._add_ps_data(txid, tx, tx_type)
                type_name = SPEC_TX_NAMES[tx_type]
                if log:
                    self.logger.info(f'Found {type_name} {txid}')
                found += 1
        w.storage.write()
        if found:
            self.trigger_callback('ps-data-updated', w)
        else:
            self.logger.info(f'No untracked PrivateSend transactions found')
        return found

    def find_common_ancestor(self, utxo_a, utxo_b, search_depth=5):
        w = self.wallet
        min_common_depth = 1e9
        cur_depth = 0
        cur_utxos_a = [(utxo_a, ())]
        cur_utxos_b = [(utxo_b, ())]
        txids_a = {}
        txids_b = {}
        while cur_depth <= search_depth:
            next_utxos_a = []
            for utxo, path in cur_utxos_a:
                txid = utxo['prevout_hash']
                txid_path = path + (txid, )
                txids_a[txid] = txid_path
                tx = w.db.get_transaction(txid)
                if tx:
                    for txin in tx.inputs():
                        txin = copy.deepcopy(txin)
                        w.add_input_info(txin)
                        addr = txin['address']
                        if addr and w.is_mine(addr):
                            next_utxos_a.append((txin, txid_path))
            cur_utxos_a = next_utxos_a[:]

            next_utxos_b = []
            for utxo, path in cur_utxos_b:
                txid = utxo['prevout_hash']
                txid_path = path + (txid, )
                txids_b[txid] = txid_path
                tx = w.db.get_transaction(txid)
                if tx:
                    for txin in tx.inputs():
                        txin = copy.deepcopy(txin)
                        w.add_input_info(txin)
                        addr = txin['address']
                        if addr and w.is_mine(addr):
                            next_utxos_b.append((txin, txid_path))
            cur_utxos_b = next_utxos_b[:]

            common_txids = set(txids_a).intersection(txids_b)
            if common_txids:
                res = {'paths_a': [], 'paths_b': []}
                for txid in common_txids:
                    path_a = txids_a[txid]
                    path_b = txids_b[txid]
                    min_common_depth = min(min_common_depth, len(path_a) - 1)
                    min_common_depth = min(min_common_depth, len(path_b) - 1)
                    res['paths_a'].append(path_a)
                    res['paths_b'].append(path_b)
                res['min_common_depth'] = min_common_depth
                return res

            cur_utxos_a = next_utxos_a[:]
            cur_depth += 1
