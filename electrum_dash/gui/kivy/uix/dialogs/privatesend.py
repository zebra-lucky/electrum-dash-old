from kivy.clock import Clock
from kivy.factory import Factory
from kivy.properties import (NumericProperty, StringProperty, BooleanProperty,
                             ObjectProperty)
from kivy.lang import Builder

from electrum_dash.gui.kivy.i18n import _
from electrum_dash.gui.kivy.uix.dialogs.dash_ps_info import PSInfoDialog


Builder.load_string('''
#:import _ electrum_dash.gui.kivy.i18n._
#:import SpinBox electrum_dash.gui.kivy.uix.spinbox.SpinBox


<KeepAmountPopup@Popup>
    title: root.title
    size_hint: 0.8, 0.5
    spinbox: spinbox
    BoxLayout:
        orientation: 'vertical'
        padding: dp(10), dp(10)
        spacing: dp(10)
        SpinBox:
            id: spinbox
            size_hint: 1, 0.7
        Button:
            text: _('Set')
            size_hint: 1, 0.3
            on_release: root.dismiss(spinbox.value)


<MixRoundsPopup@Popup>
    title: root.title
    size_hint: 0.8, 0.5
    spinbox: spinbox
    BoxLayout:
        padding: dp(10), dp(10)
        spacing: dp(10)
        orientation: 'vertical'
        SpinBox:
            id: spinbox
            size_hint: 1, 0.7
        Button:
            text: _('Set')
            size_hint: 1, 0.3
            on_release: root.dismiss(spinbox.value)


<MaxSessionsPopup@Popup>
    title: root.title
    size_hint: 0.8, 0.5
    spinbox: spinbox
    BoxLayout:
        padding: dp(10), dp(10)
        spacing: dp(10)
        orientation: 'vertical'
        SpinBox:
            id: spinbox
            size_hint: 1, 0.7
        Button:
            text: _('Set')
            size_hint: 1, 0.3
            on_release: root.dismiss(spinbox.value)


<SettingsProgress@ButtonBehavior+BoxLayout>
    orientation: 'vertical'
    title: ''
    prog_val: 0
    size_hint: 1, None
    height: self.minimum_height
    padding: 0, '10dp', 0, '10dp'
    spacing: '10dp'
    canvas.before:
        Color:
            rgba: (.2, .5, .75, 1) if self.state == 'down' else (.3, .3, .3, 0)
        Rectangle:
            size: self.size
            pos: self.pos
    on_release:
        Clock.schedule_once(self.action)
    TopLabel:
        id: title
        text: self.parent.title + ': %s%%' % self.parent.prog_val
        bold: True
        halign: 'left'
    ProgressBar:
        height: '20dp'
        size_hint: 1, None
        max: 100
        value: self.parent.prog_val


<MixingProgressPopup@Popup>
    title: root.title
    data: data
    ScrollView:
        Label:
            id: data
            text: ''
            halign: 'left'
            valign: 'top'
            size_hint_y: None
            text_size: self.width, None
            height: self.texture_size[1]
            padding: dp(5), dp(5)


<PrivateSendDialog@Popup>
    title: _('PrivateSend')
    BoxLayout:
        orientation: 'vertical'
        ScrollView:
            GridLayout:
                id: scrollviewlayout
                cols:1
                size_hint: 1, None
                height: self.minimum_height
                padding: '10dp'
                CardSeparator
                SettingsItem:
                    title: root.keep_amount_text + ': %s' % root.keep_amount
                    description: root.keep_amount_help
                    action: root.show_keep_amount_popup
                CardSeparator
                SettingsItem:
                    title: root.mix_rounds_text + ': %s' % root.mix_rounds
                    description: root.mix_rounds_help
                    action: root.show_mix_rounds_popup
                CardSeparator
                SettingsItem:
                    title: root.max_sessions_text + ': %s' % root.max_sessions
                    description: root.max_sessions_help
                    action: root.show_max_sessions_popup
                CardSeparator
                SettingsItem:
                    title: root.is_mixing_run_text
                    description: root.is_mixing_run_help
                    action: root.toggle_is_mixing_run
                CardSeparator
                SettingsProgress:
                    title: root.mix_prog_text
                    prog_val: root.mix_prog
                    action: root.show_mixing_progress_by_rounds
                CardSeparator
                SettingsItem:
                    title: root.ps_balance_text + ': ' + root.ps_balance
                    description: root.ps_balance_help
                    action: root.toggle_fiat_ps_balance
                CardSeparator
                SettingsItem:
                    title: root.dn_balance_text + ': ' + root.dn_balance
                    description: root.dn_balance_help
                    action: root.toggle_fiat_dn_balance
                CardSeparator
                SettingsItem:
                    title: _('PrivateSend Coins')
                    description: _('Show and use PrivateSend/Standard coins')
                    action: root.show_coins_dialog
                CardSeparator
                SettingsItem:
                    value: ': ON' if root.group_history else ': OFF'
                    title: root.group_history_text + self.value
                    description: root.group_history_help
                    action: root.toggle_group_history
                CardSeparator
                SettingsItem:
                    value: ': ON' if root.subscribe_spent else ': OFF'
                    title: root.subscribe_spent_text + self.value
                    description: root.subscribe_spent_help
                    action: root.toggle_sub_spent
                CardSeparator
                SettingsItem:
                    title: root.ps_debug_text
                    description: root.ps_debug_help
                    action: root.show_ps_info_popup
''')


class KeepAmountPopup(Factory.Popup):

    spinbox = ObjectProperty(None)

    def __init__(self, psdlg):
        Factory.Popup.__init__(self)
        self.psdlg = psdlg
        self.psman = psman = psdlg.psman
        self.title = self.psdlg.keep_amount_text
        self.spinbox.min_val = psman.keep_amount_data(minv=True)
        self.spinbox.max_val = psman.keep_amount_data(maxv=True)
        self.spinbox.value = self.psdlg.keep_amount

    def dismiss(self, value=None):
        if self.spinbox.err.text:
            return
        super(KeepAmountPopup, self).dismiss()
        if value is not None and value != self.psman.keep_amount:
            self.psman.keep_amount = value
            self.psdlg.keep_amount = value


class MixRoundsPopup(Factory.Popup):

    spinbox = ObjectProperty(None)

    def __init__(self, psdlg):
        Factory.Popup.__init__(self)
        self.psdlg = psdlg
        self.psman = psman = psdlg.psman
        self.title = self.psdlg.mix_rounds_text
        self.spinbox.min_val = psman.mix_rounds_data(minv=True)
        self.spinbox.max_val = psman.mix_rounds_data(maxv=True)
        self.spinbox.value = self.psdlg.mix_rounds

    def dismiss(self, value=None):
        if self.spinbox.err.text:
            return
        super(MixRoundsPopup, self).dismiss()
        if value is not None and value != self.psman.mix_rounds:
            self.psman.mix_rounds = value
            self.psdlg.mix_rounds = value
            self.psdlg.update()


class MaxSessionsPopup(Factory.Popup):

    spinbox = ObjectProperty(None)

    def __init__(self, psdlg):
        Factory.Popup.__init__(self)
        self.psdlg = psdlg
        self.psman = psman = psdlg.psman
        self.title = self.psdlg.max_sessions_text
        self.spinbox.min_val = psman.max_sessions_data(minv=True)
        self.spinbox.max_val = psman.max_sessions_data(maxv=True)
        self.spinbox.value = self.psdlg.max_sessions

    def dismiss(self, value=None):
        if self.spinbox.err.text:
            return
        super(MaxSessionsPopup, self).dismiss()
        if value is not None and value != self.psman.max_sessions:
            self.psman.max_sessions = value
            self.psdlg.max_sessions = value


class MixingProgressPopup(Factory.Popup):

    def __init__(self, psdlg):
        Factory.Popup.__init__(self)
        self.psdlg = psdlg
        self.psman = psman = psdlg.psman
        self.title = self.psdlg.mix_prog_text
        res = ''
        mix_rounds = psman.mix_rounds
        for i in range(1, mix_rounds+1):
            progress = psman.mixing_progress(i)
            res += f'Round: {i}\t Progress: {progress}%\n'
        self.data.text = res


class PrivateSendDialog(Factory.Popup):

    keep_amount = NumericProperty()
    mix_rounds = NumericProperty()
    max_sessions = NumericProperty()
    is_mixing_run_text = StringProperty()
    mix_prog = NumericProperty()
    dn_balance = StringProperty()
    ps_balance = StringProperty()
    group_history = BooleanProperty()
    subscribe_spent = BooleanProperty()
    is_fiat_dn_balance = False
    is_fiat_ps_balance = False

    def __init__(self, app):
        self.app = app
        self.config = app.electrum_config

        self.update()
        Factory.Popup.__init__(self)
        layout = self.ids.scrollviewlayout
        layout.bind(minimum_height=layout.setter('height'))

    def update(self):
        app = self.app
        self.wallet = wallet = app.wallet
        self.psman = psman = wallet.psman

        self.keep_amount = psman.keep_amount
        self.keep_amount_text = psman.keep_amount_data(short_txt=True)
        self.keep_amount_help = psman.keep_amount_data(full_txt=True)

        self.mix_rounds = psman.mix_rounds
        self.mix_rounds_text = psman.mix_rounds_data(short_txt=True)
        self.mix_rounds_help = psman.mix_rounds_data(full_txt=True)

        self.max_sessions = psman.max_sessions
        self.max_sessions_text = psman.max_sessions_data(short_txt=True)
        self.max_sessions_help = psman.max_sessions_data(full_txt=True)

        self.is_mixing_run_text = psman.is_mixing_run_data(short_txt=True)
        self.is_mixing_run_help = psman.is_mixing_run_data(full_txt=True)

        self.mix_prog = psman.mixing_progress()
        self.mix_prog_text = psman.mixing_progress_data(short_txt=True)
        self.mix_prog_help = psman.mixing_progress_data(full_txt=True)

        val = sum(wallet.get_balance(include_ps=False,
                                     min_rounds=psman.mix_rounds))
        self.ps_balance = app.format_amount_and_units(val)
        self.ps_balance_text = psman.ps_balance_data(short_txt=True)
        self.ps_balance_help = psman.ps_balance_data(full_txt=True)

        val = sum(wallet.get_balance(include_ps=False,
                                     min_rounds=0))
        self.dn_balance = app.format_amount_and_units(val)
        self.dn_balance_text = psman.dn_balance_data(short_txt=True)
        self.dn_balance_help = psman.dn_balance_data(full_txt=True)

        self.ps_debug_text = psman.ps_debug_data(short_txt=True)
        self.ps_debug_help = psman.ps_debug_data(full_txt=True)

        self.group_history = psman.group_history
        self.group_history_text = psman.group_history_data(short_txt=True)
        self.group_history_help = psman.group_history_data(full_txt=True)

        self.subscribe_spent = psman.subscribe_spent
        self.subscribe_spent_text = psman.subscribe_spent_data(short_txt=True)
        self.subscribe_spent_help = psman.subscribe_spent_data(full_txt=True)

    def open(self):
        super(PrivateSendDialog, self).open()
        self.update()
        self.psman.register_callback(self.on_ps_callback,
                                     ['ps-mixing-changes', 'ps-data-updated'])

    def dismiss(self):
        super(PrivateSendDialog, self).dismiss()
        self.psman.unregister_callback(self.on_ps_callback)

    def on_ps_callback(self, event, *args):
        Clock.schedule_once(lambda dt: self.on_ps_event(event, *args))

    def on_ps_event(self, event, *args):
        if event == 'ps-mixing-changes':
            wallet, msg = args
            if wallet == self.wallet and msg:
                self.is_mixing_run_text = \
                    self.psman.is_mixing_run_data(short_txt=True)
        elif event == 'ps-data-updated':
            wallet = args[0]
            psman = wallet.psman
            if wallet == self.wallet:
                val = sum(wallet.get_balance(include_ps=False,
                                             min_rounds=0))
                self.dn_balance = self.app.format_amount_and_units(val)
                val = sum(wallet.get_balance(include_ps=False,
                                             min_rounds=psman.mix_rounds))
                self.ps_balance = self.app.format_amount_and_units(val)
                self.mix_prog = psman.mixing_progress()

    def show_keep_amount_popup(self, *args):
        if self.psman.is_mixing_run:
            self.app.show_info(_('To change value stop mixing process'))
            return
        KeepAmountPopup(self).open(self.psman)

    def show_mix_rounds_popup(self, *args):
        if self.psman.is_mixing_run:
            self.app.show_info(_('To change value stop mixing process'))
            return
        MixRoundsPopup(self).open(self.psman)

    def show_max_sessions_popup(self, *args):
        if self.psman.is_mixing_run:
            self.app.show_info(_('To change value stop mixing process'))
            return
        MaxSessionsPopup(self).open(self.psman)

    def toggle_is_mixing_run(self, *args):
        psman = self.psman
        if psman.is_mixing_changes:
            return
        is_mixing_run = not psman.is_mixing_run
        if is_mixing_run:
            self.app.protected(_('Enter your PIN code to start mixing'),
                               self._start_mixing, ())
        else:
            psman.stop_mixing()
        self.is_mixing_run_text = psman.is_mixing_run_data(short_txt=True)

    def _start_mixing(self, password):
        psman = self.psman
        psman.start_mixing(password)

    def toggle_fiat_dn_balance(self, *args):
        self.is_fiat_dn_balance = not self.is_fiat_dn_balance
        val = sum(self.wallet.get_balance(include_ps=False, min_rounds=0))
        app = self.app
        if self.is_fiat_dn_balance:
            fiat_balance = app.fx.format_amount(val)
            ccy = app.fx.ccy
            self.dn_balance = f'{fiat_balance} {ccy}'
        else:
            self.dn_balance = app.format_amount_and_units(val)

    def toggle_fiat_ps_balance(self, *args):
        self.is_fiat_ps_balance = not self.is_fiat_ps_balance
        val = sum(self.wallet.get_balance(include_ps=False,
                                          min_rounds=self.psman.mix_rounds))
        app = self.app
        if self.is_fiat_ps_balance:
            fiat_balance = app.fx.format_amount(val)
            ccy = app.fx.ccy
            self.ps_balance = f'{fiat_balance} {ccy}'
        else:
            self.ps_balance = app.format_amount_and_units(val)

    def show_mixing_progress_by_rounds(self, *args):
        d = MixingProgressPopup(self)
        d.open()

    def show_ps_info_popup(self, *args):
        d = PSInfoDialog(self.app)
        d.open()

    def toggle_group_history(self, *args):
        self.psman.group_history = not self.psman.group_history
        self.group_history = self.psman.group_history
        self.app._trigger_update_history()

    def toggle_sub_spent(self, *args):
        self.psman.subscribe_spent = not self.psman.subscribe_spent
        self.subscribe_spent = self.psman.subscribe_spent

    def show_coins_dialog(self, *args):
        self.app.coins_dialog()
