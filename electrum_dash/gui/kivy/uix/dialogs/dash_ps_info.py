from kivy.clock import Clock
from kivy.factory import Factory
from kivy.lang import Builder
from kivy.properties import BooleanProperty
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.recycleview.layout import LayoutSelectionBehavior
from kivy.uix.recycleview.views import RecycleDataViewBehavior
from kivy.uix.recycleboxlayout import RecycleBoxLayout
from kivy.uix.behaviors import FocusBehavior

from electrum_dash.dash_ps import filter_log_line
from electrum_dash.gui.kivy.uix.dialogs.question import Question


Builder.load_string('''
<LineItem>
    text: ''
    lb: lb
    size_hint: 1, None
    height: lb.height
    canvas.before:
        Color:
            rgba: (0.192, .498, 0.745, 1) if self.selected  \
                else (0.15, 0.15, 0.17, 1)
        Rectangle:
            size: self.size
            pos: self.pos
    Label:
        id: lb
        text: root.text
        halign: 'left'
        valign: 'top'
        size_hint_y: None
        text_size: self.width, None
        height: self.texture_size[1]
        padding: dp(5), dp(5)
        font_size: '13sp'


<RecycledLinesView@RecycleView>
    dlg: None
    btns: None
    scroll_type: ['bars', 'content']
    bar_width: '15dp'
    viewclass: 'LineItem'
    LinesRecycleBoxLayout:
        dlg: root.dlg
        btns: root.btns
        orientation: 'vertical'
        size_hint_y: None
        padding: '5dp', '5dp'
        spacing: '5dp'
        height: self.minimum_height
        default_size: None, None
        default_size_hint: 1, None
        multiselect: True
        touch_multiselect: True


<ControlButtons@BoxLayout>
    sel_all: sel_all
    clear_sel: clear_sel
    copy_sel: copy_sel
    orientation: 'horizontal'
    size_hint: 1, None
    height: self.minimum_height
    Button:
        id: sel_all
        text: _('Select All')
        size_hint: 1, None
        height: '48dp'
        on_release: root.dlg.select_all(root.rv)
    Button:
        id: clear_sel
        text: _('Clear Selection')
        size_hint: 1, None
        height: '48dp'
        disabled: True
        on_release: root.dlg.clear_selection(root.rv)
    Button:
        id: copy_sel
        text: _('Copy Selected')
        size_hint: 1, None
        height: '48dp'
        disabled: True
        on_release: root.dlg.copy_selected(root.rv)


<PSInfoDialog@Popup>
    title: ''
    id: dlg
    log_rv: log_rv
    log_rv_btns: log_rv_btns
    info_rv: info_rv
    info_rv_btns: info_rv_btns
    TabbedPanel:
        do_default_tab: False
        TabbedPanelItem:
            text: _('Log')
            BoxLayout:
                orientation: 'vertical'
                RecycledLinesView:
                    id: log_rv
                    dlg: dlg
                    btns: log_rv_btns
                    scroll_y: 0
                    effect_cls: 'ScrollEffect'
                ControlButtons
                    id: log_rv_btns
                    dlg: dlg
                    rv: log_rv
                BoxLayout:
                    orientation: 'horizontal'
                    size_hint: 1, None
                    height: self.minimum_height
                    Button:
                        text: _('Clear Log')
                        size_hint: 1, None
                        height: '48dp'
                        on_release: root.clear_log(self)
        TabbedPanelItem:
            text: _('Info')
            BoxLayout:
                orientation: 'vertical'
                RecycledLinesView:
                    id: info_rv
                    dlg: dlg
                    btns: info_rv_btns
                ControlButtons
                    id: info_rv_btns
                    dlg: dlg
                    rv: info_rv
                BoxLayout:
                    orientation: 'horizontal'
                    size_hint: 1, None
                    height: self.minimum_height
                    Button:
                        text: _('Clear PS data')
                        size_hint: 0.5, None
                        height: '48dp'
                        on_release: root.clear_ps_data(self)
                    Button:
                        text: _('Find untracked PS txs')
                        size_hint: 0.5, None
                        height: '48dp'
                        on_release: root.find_untracked_ps_txs(self)
''')


class LineItem(RecycleDataViewBehavior, BoxLayout):
    index = None
    selected = BooleanProperty(False)

    def refresh_view_attrs(self, rv, index, data):
        self.index = index
        return super(LineItem, self).refresh_view_attrs(rv, index, data)

    def on_touch_down(self, touch):
        if super(LineItem, self).on_touch_down(touch):
            return True
        if self.collide_point(*touch.pos):
            return self.parent.select_with_touch(self.index, touch)

    def apply_selection(self, rv, index, is_selected):
        self.selected = is_selected


class LinesRecycleBoxLayout(FocusBehavior, LayoutSelectionBehavior,
                            RecycleBoxLayout):
    def select_node(self, node):
        super(LinesRecycleBoxLayout, self).select_node(node)
        if self.selected_nodes:
            self.btns.clear_sel.disabled = False
            self.btns.copy_sel.disabled = False

    def deselect_node(self, node):
        super(LinesRecycleBoxLayout, self).deselect_node(node)
        if not self.selected_nodes:
            self.btns.clear_sel.disabled = True
            self.btns.copy_sel.disabled = True


class PSInfoDialog(Factory.Popup):

    def __init__(self, app):
        Factory.Popup.__init__(self)
        self.app = app
        self.wallet = app.wallet
        self.psman = psman = app.wallet.psman
        self.title = psman.ps_debug_data(short_txt=True)

    def open(self):
        super(PSInfoDialog, self).open()
        psman = self.psman
        log_handler = psman.log_handler
        log_handler.acquire()
        try:
            psman.debug = True
            psman.register_callback(self.on_ps_callback, ['log-line',
                                                          'ps-data-updated'])
            data = [{'text': line} for line in log_handler.log_deque]
            self.log_rv.data = data
        finally:
            log_handler.release()
        self.update()

    def dismiss(self):
        super(PSInfoDialog, self).dismiss()
        self.psman.debug = False
        self.psman.unregister_callback(self.on_ps_callback)

    def on_ps_callback(self, event, *args):
        Clock.schedule_once(lambda dt: self.on_ps_event(event, *args))

    def on_ps_event(self, event, *args):
        if event == 'log-line':
            psman, log_line = args
            if psman == self.psman:
                self.log_rv.data.append({'text': log_line})
        elif event == 'ps-data-updated':
            wallet = args[0]
            if wallet == self.wallet:
                self.update()

    def update(self):
        lines = self.psman.get_ps_data_info()
        self.info_rv.data = [{'text': line} for line in lines]

    def select_all(self, rv):
        for i in range(len(rv.data)):
            rv.layout_manager.select_node(i)

    def clear_selection(self, rv):
        rv.layout_manager.clear_selection()

    def copy_selected(self, rv):
        res = []
        for i in sorted(rv.layout_manager.selected_nodes):
            line = filter_log_line(rv.data[i]['text'])
            res.append(line)
        self.app._clipboard.copy('\n'.join(res))

    def clear_log(self, btn):
        psman = self.psman
        btn.disabled = True

        def _clear_log():
            log_handler = psman.log_handler
            log_handler.acquire()
            try:
                psman.log_handler.clear_log()
                self.log_rv.data = []
            finally:
                log_handler.release()
            btn.disabled = False
        Clock.schedule_once(lambda dt: _clear_log())

    def clear_ps_data(self, btn):
        psman = self.psman
        btn.disabled = True

        def _clear_ps_data(b: bool):
            if b:
                psman.clear_ps_data()
            btn.disabled = False
        d = Question(psman.CLEAR_PS_DATA_MSG, _clear_ps_data)
        d.open()

    def find_untracked_ps_txs(self, btn):
        psman = self.psman
        btn.disabled = True

        def _find_untracked_ps_txs():
            psman.find_untracked_ps_txs()
            btn.disabled = False
        Clock.schedule_once(lambda dt: _find_untracked_ps_txs())
