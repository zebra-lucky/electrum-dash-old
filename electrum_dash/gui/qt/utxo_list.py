#!/usr/bin/env python
#
# Electrum - lightweight Bitcoin client
# Copyright (C) 2015 Thomas Voegtlin
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

from typing import Optional, List
from enum import IntEnum

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QStandardItemModel, QStandardItem, QFont
from PyQt5.QtWidgets import QAbstractItemView, QMenu, QComboBox, QLabel

from electrum_dash.i18n import _
from electrum_dash.dash_ps import sort_utxos_by_ps_rounds
from electrum_dash.dash_tx import PSCoinRounds, SPEC_TX_NAMES

from .util import MyTreeView, ColorScheme, MONOSPACE_FONT

class UTXOList(MyTreeView):

    class Columns(IntEnum):
        OUTPOINT = 0
        ADDRESS = 1
        LABEL = 2
        AMOUNT = 3
        HEIGHT = 4
        PS_ROUNDS = 5

    headers = {
        Columns.ADDRESS: _('Address'),
        Columns.LABEL: _('Label'),
        Columns.PS_ROUNDS: _('PS Rounds'),
        Columns.AMOUNT: _('Amount'),
        Columns.HEIGHT: _('Height'),
        Columns.OUTPOINT: _('Output point'),
    }
    filter_columns = [Columns.ADDRESS, Columns.PS_ROUNDS,
                      Columns.LABEL, Columns.OUTPOINT]

    def __init__(self, parent=None):
        super().__init__(parent, self.create_menu,
                         stretch_column=self.Columns.LABEL,
                         editable_columns=[])
        self.setModel(QStandardItemModel(self))
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setSortingEnabled(True)
        self.show_ps = 0
        self.ps_button = QComboBox(self)
        self.ps_button.currentIndexChanged.connect(self.toggle_ps)
        for t in [_('All'), _('PrivateSend'), _('Regular')]:
            self.ps_button.addItem(t)
        self.update()

    def get_toolbar_buttons(self):
        return QLabel(_("Filter:")), self.ps_button

    def on_hide_toolbar(self):
        self.show_ps = 0
        self.update()

    def save_toolbar_state(self, state, config):
        config.set_key('show_toolbar_utxos', state)

    def toggle_ps(self, state):
        if state == self.show_ps:
            return
        self.show_ps = state
        self.update()

    def update(self):
        self.wallet = self.parent.wallet
        if self.show_ps == 0:  # All
            utxos = self.wallet.get_utxos(include_ps=True)
        elif self.show_ps == 1:  # PrivateSend
            utxos = self.wallet.get_utxos(min_rounds=PSCoinRounds.MINUSINF)
        else:  # Regular
            utxos = self.wallet.get_utxos()
        utxos.sort(key=sort_utxos_by_ps_rounds)
        self.utxo_dict = {}
        self.model().clear()
        self.update_headers(self.__class__.headers)
        for idx, x in enumerate(utxos):
            self.insert_utxo(idx, x)
        self.filter()

    def insert_utxo(self, idx, x):
        address = x['address']
        ps_rounds = x['ps_rounds']
        if ps_rounds is None:
            ps_rounds = 'N/A'
        elif ps_rounds == PSCoinRounds.COLLATERAL:
            ps_rounds = 'Collateral'
        elif ps_rounds == PSCoinRounds.OTHER:
            ps_rounds = 'Other'
        else:
            ps_rounds = str(ps_rounds)
        height = x.get('height')
        name = x.get('prevout_hash') + ":%d"%x.get('prevout_n')
        name_short = x.get('prevout_hash')[:16] + '...' + ":%d"%x.get('prevout_n')
        self.utxo_dict[name] = x
        label = self.wallet.get_label(x.get('prevout_hash'))
        amount = self.parent.format_amount(x['value'], whitespaces=True)
        labels = [name_short, address, label, amount, '%d'%height, ps_rounds]
        utxo_item = [QStandardItem(x) for x in labels]
        self.set_editability(utxo_item)
        for i in range(6):
            utxo_item[i].setFont(QFont(MONOSPACE_FONT))
        utxo_item[self.Columns.PS_ROUNDS].setTextAlignment(Qt.AlignRight)
        utxo_item[self.Columns.AMOUNT].setTextAlignment(Qt.AlignRight)
        utxo_item[self.Columns.HEIGHT].setTextAlignment(Qt.AlignRight)
        utxo_item[self.Columns.ADDRESS].setData(name, Qt.UserRole)
        if self.wallet.is_frozen_address(address):
            utxo_item[self.Columns.ADDRESS].setBackground(ColorScheme.BLUE.as_color(True))
            utxo_item[self.Columns.ADDRESS].setToolTip(_('Address is frozen'))
        if self.wallet.is_frozen_coin(x):
            utxo_item[self.Columns.OUTPOINT].setBackground(ColorScheme.BLUE.as_color(True))
            utxo_item[self.Columns.OUTPOINT].setToolTip(f"{name}\n{_('Coin is frozen')}")
        else:
            utxo_item[self.Columns.OUTPOINT].setToolTip(name)
        self.model().insertRow(idx, utxo_item)

    def get_selected_outpoints(self) -> Optional[List[str]]:
        if not self.model():
            return None
        items = self.selected_in_column(self.Columns.ADDRESS)
        if not items:
            return None
        return [x.data(Qt.UserRole) for x in items]

    def create_menu(self, position):
        selected = self.get_selected_outpoints()
        if not selected:
            return
        menu = QMenu()
        menu.setSeparatorsCollapsible(True)  # consecutive separators are merged together
        coins = [self.utxo_dict[name] for name in selected]
        menu.addAction(_("Spend"), lambda: self.parent.spend_coins(coins))
        assert len(coins) >= 1, len(coins)
        if len(coins) == 1:
            utxo_dict = coins[0]
            ps_rounds = utxo_dict['ps_rounds']
            addr = utxo_dict['address']
            txid = utxo_dict['prevout_hash']
            if ps_rounds is not None:
                if ps_rounds == PSCoinRounds.OTHER:
                    menu.addAction(_('Create New Denoms'),
                                   lambda: self.create_new_denoms(coins))
                elif ps_rounds >= 0:
                    menu.addAction(_('Create New Collateral'),
                                   lambda: self.create_new_collateral(coins))
            # "Details"
            tx = self.wallet.db.get_transaction(txid)
            if tx:
                label = self.wallet.get_label(txid) or None # Prefer None if empty (None hides the Description: field in the window)
                menu.addAction(_("Details"), lambda: self.parent.show_transaction(tx, label))
            # "Copy ..."
            idx = self.indexAt(position)
            if not idx.isValid():
                return
            col = idx.column()
            column_title = self.model().horizontalHeaderItem(col).text()
            copy_text = self.model().itemFromIndex(idx).text() if col != self.Columns.OUTPOINT else selected[0]
            if col == self.Columns.AMOUNT:
                copy_text = copy_text.strip()
            menu.addAction(_("Copy {}").format(column_title), lambda: self.parent.app.clipboard().setText(copy_text))
            if ps_rounds is not None:
                menu.exec_(self.viewport().mapToGlobal(position))
                return
            # "Freeze coin"
            if not self.wallet.is_frozen_coin(utxo_dict):
                menu.addAction(_("Freeze Coin"), lambda: self.parent.set_frozen_state_of_coins([utxo_dict], True))
            else:
                menu.addSeparator()
                menu.addAction(_("Coin is frozen"), lambda: None).setEnabled(False)
                menu.addAction(_("Unfreeze Coin"), lambda: self.parent.set_frozen_state_of_coins([utxo_dict], False))
                menu.addSeparator()
            # "Freeze address"
            if not self.wallet.is_frozen_address(addr):
                menu.addAction(_("Freeze Address"), lambda: self.parent.set_frozen_state_of_addresses([addr], True))
            else:
                menu.addSeparator()
                menu.addAction(_("Address is frozen"), lambda: None).setEnabled(False)
                menu.addAction(_("Unfreeze Address"), lambda: self.parent.set_frozen_state_of_addresses([addr], False))
                menu.addSeparator()
        else:
            ps_rounds = set([utxo_dict['ps_rounds'] for utxo_dict in coins])
            if None not in ps_rounds:
                if ps_rounds == {int(PSCoinRounds.OTHER)}:
                    menu.addAction(_('Create New Denoms'),
                                   lambda: self.create_new_denoms(coins))
                menu.exec_(self.viewport().mapToGlobal(position))
                return
            elif len(ps_rounds) > 1:
                menu.exec_(self.viewport().mapToGlobal(position))
                return
            # multiple items selected
            menu.addSeparator()
            addrs = [utxo_dict['address'] for utxo_dict in coins]
            is_coin_frozen = [self.wallet.is_frozen_coin(utxo_dict) for utxo_dict in coins]
            is_addr_frozen = [self.wallet.is_frozen_address(utxo_dict['address']) for utxo_dict in coins]
            if not all(is_coin_frozen):
                menu.addAction(_("Freeze Coins"), lambda: self.parent.set_frozen_state_of_coins(coins, True))
            if any(is_coin_frozen):
                menu.addAction(_("Unfreeze Coins"), lambda: self.parent.set_frozen_state_of_coins(coins, False))
            if not all(is_addr_frozen):
                menu.addAction(_("Freeze Addresses"), lambda: self.parent.set_frozen_state_of_addresses(addrs, True))
            if any(is_addr_frozen):
                menu.addAction(_("Unfreeze Addresses"), lambda: self.parent.set_frozen_state_of_addresses(addrs, False))

        menu.exec_(self.viewport().mapToGlobal(position))

    def confirm_wfl_transactions(self, wfl):
        mwin = self.parent
        psman = mwin.wallet.psman
        tx_type, tx_cnt, total, total_fee = psman.get_workflow_tx_info(wfl)
        tx_type_name = SPEC_TX_NAMES[tx_type]
        total = mwin.format_amount_and_units(total)
        total_fee = mwin.format_amount_and_units(total_fee)
        q = _('Do you want to send "{}" transactions?').format(tx_type_name)
        q += '\n\n'
        q += _('Count of transactions: {}').format(tx_cnt)
        q += '\n'
        q += _('Total sent amount: {}').format(total)
        q += '\n'
        q += _('Total fee: {}').format(total_fee)
        return mwin.question(q)

    def create_new_denoms(self, coins):
        w = self.parent.wallet
        psman = w.psman
        wfl, err = self.parent.create_new_denoms_wfl_from_gui(coins)
        if err:
            self.parent.show_error(err)
        elif not self.confirm_wfl_transactions(wfl):
            psman._cleanup_new_denoms_wfl(wfl, force=True)
        else:
            for txid in wfl.tx_order:
                tx = w.db.get_transaction(txid)
                if tx:
                    self.parent.broadcast_transaction(tx, None)

    def create_new_collateral(self, coins):
        w = self.parent.wallet
        psman = w.psman
        wfl, err = self.parent.create_new_collateral_wfl_from_gui(coins)
        if err:
            self.parent.show_error(err)
        elif not self.confirm_wfl_transactions(wfl):
            psman._cleanup_new_collateral_wfl(wfl, force=True)
        else:
            for txid in wfl.tx_order:
                tx = w.db.get_transaction(txid)
                if tx:
                    self.parent.broadcast_transaction(tx, None)
