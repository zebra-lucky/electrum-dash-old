# -*- coding: utf-8 -*-

from PyQt5.QtCore import Qt, QRect, QPoint, QSize
from PyQt5.QtGui import QTextCursor
from PyQt5.QtWidgets import (QTabBar, QTextEdit, QCheckBox, QStylePainter,
                             QStyleOptionTab, QStyle, QSpinBox, QVBoxLayout,
                             QPushButton, QLabel, QDialog, QGridLayout,
                             QTabWidget, QWidget, QProgressBar, QHBoxLayout)

from electrum_dash.dash_tx import SPEC_TX_NAMES
from electrum_dash.i18n import _

from .util import HelpLabel, MessageBoxMixin, read_QIcon


def get_ps_tab_widgets(mwin):
    w = []

    wallet = mwin.wallet
    psman = wallet.psman
    if not psman.enabled:
        ps_disabled_label = QLabel(psman.disabled_msg)
        ps_disabled_label.setWordWrap(True)
        w.append((ps_disabled_label, None))

        def dlg_on_ps_signal(event, args):
            pass
        return w, dlg_on_ps_signal

    # keep_amount
    keep_amount_text = psman.keep_amount_data(short_txt=True)
    keep_amount_help = psman.keep_amount_data(full_txt=True)
    keep_amount_label = HelpLabel(keep_amount_text + ':', keep_amount_help)
    keep_amount_sb = QSpinBox()
    keep_amount_sb.setMinimum(psman.keep_amount_data(minv=True))
    keep_amount_sb.setMaximum(psman.keep_amount_data(maxv=True))
    keep_amount_sb.setValue(psman.keep_amount)

    def on_keep_amount_change():
        psman.keep_amount = keep_amount_sb.value()
    keep_amount_sb.valueChanged.connect(on_keep_amount_change)

    w.append((keep_amount_label, keep_amount_sb))

    # mix_rounds
    mix_rounds_text = psman.mix_rounds_data(short_txt=True)
    mix_rounds_help = psman.mix_rounds_data(full_txt=True)
    mix_rounds_label = HelpLabel(mix_rounds_text + ':', mix_rounds_help)
    mix_rounds_sb = QSpinBox()
    mix_rounds_sb.setMinimum(psman.mix_rounds_data(minv=True))
    mix_rounds_sb.setMaximum(psman.mix_rounds_data(maxv=True))
    mix_rounds_sb.setValue(psman.mix_rounds)

    def on_mix_rounds_change():
        update_balances()
        psman.mix_rounds = mix_rounds_sb.value()
    mix_rounds_sb.valueChanged.connect(on_mix_rounds_change)

    w.append((mix_rounds_label, mix_rounds_sb))

    # max_sessions
    max_sessions_text = psman.max_sessions_data(short_txt=True)
    max_sessions_help = psman.max_sessions_data(full_txt=True)
    max_sessions_label = HelpLabel(max_sessions_text + ':', max_sessions_help)
    max_sessions_sb = QSpinBox()
    max_sessions_sb.setMinimum(psman.max_sessions_data(minv=True))
    max_sessions_sb.setMaximum(psman.max_sessions_data(maxv=True))
    max_sessions_sb.setValue(psman.max_sessions)

    def on_max_sessions_change():
        psman.max_sessions = max_sessions_sb.value()
    max_sessions_sb.valueChanged.connect(on_max_sessions_change)

    w.append((max_sessions_label, max_sessions_sb))

    # is_mixing_run
    is_mixing_run_btn = QPushButton(psman.is_mixing_run_data(short_txt=True))
    is_mixing_run_btn.setCheckable(True)

    def is_mixing_run_btn_pressed():
        is_mixing_run = not psman.is_mixing_run
        if is_mixing_run:
            mwin.start_mixing()
        else:
            psman.stop_mixing()
    is_mixing_run_btn.clicked.connect(is_mixing_run_btn_pressed)

    w.append((is_mixing_run_btn, None))

    # mixing progress
    mix_progress_text = psman.mixing_progress_data(short_txt=True)
    mix_progress_help = psman.mixing_progress_data(full_txt=True)
    mix_progress_label = HelpLabel(mix_progress_text + ':', mix_progress_help)
    mix_progress_bar = QProgressBar()
    w.append((mix_progress_label, mix_progress_bar))

    # ps balance
    ps_balance_text = psman.ps_balance_data(short_txt=True)
    ps_balance_help = psman.ps_balance_data(full_txt=True)
    ps_balance_label = HelpLabel(ps_balance_text + ':', ps_balance_help)
    ps_balance_amount = QLabel()
    w.append((ps_balance_label, ps_balance_amount))

    # denominated balance
    dn_balance_text = psman.dn_balance_data(short_txt=True)
    dn_balance_help = psman.dn_balance_data(full_txt=True)
    dn_balance_label = HelpLabel(dn_balance_text + ':', dn_balance_help)
    dn_balance_amount = QLabel()
    w.append((dn_balance_label, dn_balance_amount))

    # group_history
    group_hist_cb = QCheckBox(psman.group_history_data(full_txt=True))
    group_hist_cb.setChecked(psman.group_history)

    def on_group_hist_state_changed(x):
        psman.group_history = (x == Qt.Checked)
        mwin.history_model.refresh('on_grouping_change')
    group_hist_cb.stateChanged.connect(on_group_hist_state_changed)

    w.append((group_hist_cb, None))

    # notify_ps_txs
    notify_txs_cb = QCheckBox(psman.notify_ps_txs_data(full_txt=True))
    notify_txs_cb.setChecked(psman.notify_ps_txs)

    def on_notify_txs_state_changed(x):
        psman.notify_ps_txs = (x == Qt.Checked)
    notify_txs_cb.stateChanged.connect(on_notify_txs_state_changed)

    w.append((notify_txs_cb, None))

    # subscribe_spent
    sub_spent_cb = QCheckBox(psman.subscribe_spent_data(full_txt=True))
    sub_spent_cb.setChecked(psman.subscribe_spent)

    def on_sub_spent_state_changed(x):
        psman.subscribe_spent = (x == Qt.Checked)
    sub_spent_cb.stateChanged.connect(on_sub_spent_state_changed)

    w.append((sub_spent_cb, None))

    # mixing logs
    ps_debug_btn = QPushButton(psman.ps_debug_data(short_txt=True))
    ps_debug_btn.clicked.connect(lambda: show_ps_info(mwin))
    w.append((ps_debug_btn, None))

    def update_mixing_status():
        if psman.is_mixing_changes:
            is_mixing_run_btn.setEnabled(False)
        else:
            is_mixing_run_btn.setEnabled(True)
            if psman.is_mixing_run:
                keep_amount_sb.setEnabled(False)
                mix_rounds_sb.setEnabled(False)
                max_sessions_sb.setEnabled(False)
            else:
                keep_amount_sb.setEnabled(True)
                mix_rounds_sb.setEnabled(True)
                max_sessions_sb.setEnabled(True)
            is_mixing_run_btn.setText(psman.is_mixing_run_data(short_txt=True))
            is_mixing_run_btn.setChecked(psman.is_mixing_run)
    update_mixing_status()

    def update_balances():
        dn_balance = wallet.get_balance(include_ps=False, min_rounds=0)
        dn_amount = mwin.format_amount_and_units(sum(dn_balance))
        dn_balance_amount.setText(dn_amount)
        ps_balance = wallet.get_balance(include_ps=False,
                                        min_rounds=psman.mix_rounds)
        ps_amount = mwin.format_amount_and_units(sum(ps_balance))
        ps_balance_amount.setText(ps_amount)
        mix_progress_bar.setValue(psman.mixing_progress())
    update_balances()

    def dlg_on_ps_signal(event, args):
        if event == 'ps-mixing-changes':
            wallet, msg = args
            if wallet == mwin.wallet:
                update_mixing_status()
        elif event == 'ps-data-updated':
            wallet = args[0]
            if wallet == mwin.wallet:
                update_balances()

    return w, dlg_on_ps_signal


ps_info_dialogs = []  # Otherwise python randomly garbage collects the dialogs


def show_ps_info(parent):
    if ps_info_dialogs:
        d = ps_info_dialogs[0]
        d.raise_()
        d.activateWindow()
    else:
        d = PSInfoDialog(parent)
        d.show()
        ps_info_dialogs.append(d)


class PSInfoDialog(QDialog, MessageBoxMixin):

    def __init__(self, parent):
        QDialog.__init__(self, parent=None)
        self.setMinimumWidth(640)
        self.setMinimumHeight(480)
        self.setWindowIcon(read_QIcon('electrum-dash.png'))
        self.mwin = parent
        self.wallet = parent.wallet
        self.psman = psman = parent.wallet.psman
        title = psman.ps_debug_data(short_txt=True)
        self.setWindowTitle(title)

        layout = QGridLayout()
        self.setLayout(layout)
        self.tabs = QTabWidget(self)
        self.close_btn = b = QPushButton(_('Close'))
        b.setDefault(True)
        b.clicked.connect(self.close)
        layout.addWidget(self.tabs, 0, 0, 1, -1)
        layout.setColumnStretch(0, 1)
        layout.addWidget(b, 1, 1)

        self.log_tab = QWidget()
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)

        clear_log_btn = QPushButton(_('Clear Log'))
        log_handler = psman.log_handler

        def clear_log():
            clear_log_btn.setEnabled(False)
            log_handler.acquire()
            try:
                psman.log_handler.clear_log()
                self.log_view.clear()
            finally:
                log_handler.release()
            clear_log_btn.setEnabled(True)
        clear_log_btn.clicked.connect(clear_log)

        log_vbox = QVBoxLayout()
        log_vbox.addWidget(self.log_view)
        log_vbox.addWidget(clear_log_btn)
        self.log_tab.setLayout(log_vbox)

        self.ps_info_tab = QWidget()
        self.ps_info_view = QTextEdit()
        self.ps_info_view.setReadOnly(True)

        btns_hbox = QHBoxLayout()
        btns = QWidget()
        btns.setLayout(btns_hbox)

        clear_ps_data_bnt = QPushButton(_('Clear PS data'))

        def clear_ps_data():
            clear_ps_data_bnt.setEnabled(False)
            if self.question(psman.CLEAR_PS_DATA_MSG):
                psman.clear_ps_data()
            clear_ps_data_bnt.setEnabled(True)
        clear_ps_data_bnt.clicked.connect(clear_ps_data)

        btns_hbox.addWidget(clear_ps_data_bnt)

        find_untracked_ps_data_bnt = QPushButton(_('Find untracked PS txs'))

        def find_untracked_ps_txs():
            find_untracked_ps_data_bnt.setEnabled(False)
            find_untracked_ps_data_bnt.repaint()
            psman.find_untracked_ps_txs()
            find_untracked_ps_data_bnt.setEnabled(True)
        find_untracked_ps_data_bnt.clicked.connect(find_untracked_ps_txs)

        btns_hbox.addWidget(find_untracked_ps_data_bnt)

        ps_info_vbox = QVBoxLayout()
        ps_info_vbox.addWidget(self.ps_info_view)
        ps_info_vbox.addWidget(btns)
        self.ps_info_tab.setLayout(ps_info_vbox)

        self.tabs.addTab(self.log_tab, _('Log'))
        self.tabs.addTab(self.ps_info_tab, _('Info'))

        log_handler.acquire()
        try:
            psman.debug = True
            self.mwin.ps_signal.connect(self.on_ps_signal)
            self.log_view.setText(log_handler.get_log())
            cursor = self.log_view.textCursor()
            cursor.movePosition(QTextCursor.End)
            self.log_view.setTextCursor(cursor)
            self.log_view.ensureCursorVisible()
        finally:
            log_handler.release()
        self.update()

    def reject(self):
        self.close()

    def closeEvent(self, event):
        self.mwin.ps_signal.disconnect(self.on_ps_signal)
        self.psman.debug = False
        event.accept()
        try:
            ps_info_dialogs.remove(self)
        except ValueError:
            pass

    def update(self):
        lines = self.psman.get_ps_data_info()
        self.ps_info_view.setText('\n'.join(lines))

    def on_ps_signal(self, event, args):
        if event == 'log-line':
            psman, log_line = args
            if psman == self.psman:
                self.log_view.append(log_line)
                vert_sb = self.log_view.verticalScrollBar()
                if vert_sb.value() == vert_sb.maximum():
                    cursor = self.log_view.textCursor()
                    cursor.movePosition(QTextCursor.End)
                    self.log_view.setTextCursor(cursor)
                    self.log_view.ensureCursorVisible()
        elif event == 'ps-data-updated':
            wallet = args[0]
            if wallet == self.wallet:
                self.update()


class ExtraPayloadWidget(QTextEdit):
    def __init__(self, parent=None):
        super(ExtraPayloadWidget, self).__init__(parent)
        self.setReadOnly(True)
        self.tx_type = 0
        self.extra_payload = b''

    def clear(self):
        super(ExtraPayloadWidget, self).clear()
        self.tx_type = 0
        self.extra_payload = b''
        self.setText('')

    def get_extra_data(self):
        return self.tx_type, self.extra_payload

    def set_extra_data(self, tx_type, extra_payload):
        self.tx_type, self.extra_payload = tx_type, extra_payload
        tx_type_name = SPEC_TX_NAMES.get(tx_type, str(tx_type))
        self.setText('Tx Type: %s\n\n%s' % (tx_type_name, extra_payload))


class VTabBar(QTabBar):

    def tabSizeHint(self, index):
        s = QTabBar.tabSizeHint(self, index)
        s.transpose()
        return QSize(s.width(), s.height())

    def paintEvent(self, event):
        painter = QStylePainter(self)
        opt = QStyleOptionTab()

        for i in range(self.count()):
            self.initStyleOption(opt, i)
            painter.drawControl(QStyle.CE_TabBarTabShape, opt)
            painter.save()

            s = opt.rect.size()
            s.transpose()
            r = QRect(QPoint(), s)
            r.moveCenter(opt.rect.center())
            opt.rect = r

            c = self.tabRect(i).center()
            painter.translate(c)
            painter.rotate(270)
            painter.translate(-c)
            painter.drawControl(QStyle.CE_TabBarTabLabel, opt)
            painter.restore()
