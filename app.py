#!/usr/bin/python3
"""Native Ubuntu GNOME monitor layout editor and persistent keyboard profiles."""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import sys
import tempfile
import time
import uuid

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

import core


CSS = '''
window { background: @window_bg_color; }
.sidebar { background: @view_bg_color; border-right: 1px solid alpha(@borders, .5); }
.profile-row { padding: 12px 10px; border-radius: 10px; margin: 3px 8px; }
.profile-row:selected { background: alpha(@accent_bg_color, .16); color: @accent_color; }
.profile-row label { padding: 1px 0; }
.eyebrow { font-size: 10px; font-weight: 800; letter-spacing: 1.5px; color: @accent_color; }
.main-title { font-size: 28px; font-weight: 800; }
.muted { opacity: .64; }
.canvas-card { border-radius: 16px; background: #141e2b; }
.panel { background: @card_bg_color; border: 1px solid alpha(@borders, .5); border-radius: 12px; padding: 14px; }
.status-pill { background: alpha(@success_color, .12); color: @success_color; padding: 5px 10px; border-radius: 20px; font-size: 11px; }
.shortcut-key { font-size: 24px; font-weight: 700; padding: 28px; }
.suggested-action { font-weight: 700; }
.footer-note { font-size: 11px; opacity: .65; }
'''


def box(orientation=Gtk.Orientation.VERTICAL, spacing=8, **kwargs):
    return Gtk.Box(orientation=orientation, spacing=spacing, **kwargs)


def label(text, css=None, **kwargs):
    widget = Gtk.Label(label=text, xalign=0, **kwargs)
    if css:
        widget.add_css_class(css)
    return widget


def button(text, callback, css=None, icon=None):
    widget = Gtk.Button(label=text)
    if icon:
        content = box(Gtk.Orientation.HORIZONTAL, 6)
        content.append(Gtk.Image.new_from_icon_name(icon))
        content.append(label(text))
        widget.set_child(content)
    if css:
        widget.add_css_class(css)
    widget.connect('clicked', callback)
    return widget


def accelerator_label(value):
    if not value:
        return 'Sem atalho'
    valid, key, mods = Gtk.accelerator_parse(value)
    return Gtk.accelerator_get_label(key, mods) if valid else value


def set_strings(dropdown, strings):
    model = dropdown.get_model()
    current = [model.get_string(i) for i in range(model.get_n_items())] if isinstance(model, Gtk.StringList) else None
    if current != strings:
        dropdown.set_model(Gtk.StringList.new(strings))


def capture_accelerator(display, keyval, keycode, state, layout_index=0):
    mods = state & Gtk.accelerator_get_default_mod_mask()
    # Resolve the unshifted key, so Ctrl+Shift+2 is stored as 2, not as @ or ".
    ok, keys, values = display.map_keycode(keycode)
    if ok:
        base = next((v for k, v in zip(keys, values) if k.group == layout_index and k.level == 0), None)
        if base is not None:
            keyval = base
    keyval = Gdk.keyval_to_lower(keyval)
    if not Gtk.accelerator_valid(keyval, mods):
        return None
    if not mods & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SUPER_MASK):
        return None
    return Gtk.accelerator_name(keyval, mods)


class ShortcutRecorder(Gtk.Window):
    def __init__(self, parent, initial, callback):
        super().__init__(title='Gravar combinação de teclas', transient_for=parent, modal=True,
                         default_width=440, resizable=False)
        self.callback = callback
        self.value = initial
        self.surface = None
        content = box(spacing=14, margin_top=24, margin_bottom=24, margin_start=24, margin_end=24)
        content.append(label('Pressiona o teu atalho', 'title-2'))
        content.append(label('Usa Ctrl, Shift, Alt ou Super com outra tecla.', 'muted'))
        self.preview = Gtk.Label(label=accelerator_label(initial) if initial else 'À espera das teclas…')
        self.preview.add_css_class('shortcut-key')
        content.append(self.preview)
        self.note = label('Esc cancela · As teclas só são captadas nesta janela.', 'footer-note', wrap=True)
        content.append(self.note)
        actions = box(Gtk.Orientation.HORIZONTAL, 8, halign=Gtk.Align.END)
        actions.append(button('Cancelar', lambda *_: self.close()))
        self.accept = button('Usar atalho', self.accept_value, 'suggested-action')
        self.accept.set_sensitive(bool(initial))
        actions.append(self.accept)
        content.append(actions)
        self.set_child(content)
        controller = Gtk.EventControllerKey.new()
        controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        controller.connect('key-pressed', self.key_pressed)
        self.add_controller(controller)
        self.connect('map', self.inhibit)
        self.connect('close-request', self.restore)
        self.connect('unmap', self.restore)

    def inhibit(self, *_):
        self.surface = self.get_surface()
        if isinstance(self.surface, Gdk.Toplevel):
            self.surface.inhibit_system_shortcuts(None)

    def restore(self, *_):
        if self.surface and isinstance(self.surface, Gdk.Toplevel):
            self.surface.restore_system_shortcuts()
            self.surface = None
        return False

    def key_pressed(self, controller, keyval, keycode, state):
        if keyval == Gdk.KEY_Escape:
            self.close()
            return True
        event = controller.get_current_event()
        value = capture_accelerator(self.get_display(), keyval, keycode, state,
                                    event.get_layout() if isinstance(event, Gdk.KeyEvent) else 0)
        if value:
            self.value = value
            self.preview.set_label(accelerator_label(value))
            self.accept.set_sensitive(True)
        return True

    def accept_value(self, *_):
        self.restore()
        self.callback(self.value)
        self.close()


class LayoutCanvas(Gtk.DrawingArea):
    def __init__(self, window, miniature=False):
        super().__init__(hexpand=True)
        self.window = window
        self.selected = None
        self.factor, self.origin_x, self.origin_y = .1, 0, 0
        self.rects = {}
        self.dragging = None
        self.set_content_height(280)
        self.set_draw_func(self.draw)
        self.add_css_class('canvas-card')
        self.set_cursor_from_name('grab')
        drag = Gtk.GestureDrag.new()
        drag.connect('drag-begin', self.drag_begin)
        drag.connect('drag-update', self.drag_update)
        drag.connect('drag-end', self.drag_end)
        self.add_controller(drag)

    def rounded(self, cr, x, y, w, h, radius=10):
        import math
        cr.new_sub_path()
        cr.arc(x + w - radius, y + radius, radius, -math.pi / 2, 0)
        cr.arc(x + w - radius, y + h - radius, radius, 0, math.pi / 2)
        cr.arc(x + radius, y + h - radius, radius, math.pi / 2, math.pi)
        cr.arc(x + radius, y + radius, radius, math.pi, 3 * math.pi / 2)
        cr.close_path()

    def text(self, cr, text, x, y, size=12, color=(1, 1, 1), bold=False):
        cr.select_font_face('Sans', 0, 1 if bold else 0)
        cr.set_font_size(size)
        cr.set_source_rgb(*color)
        cr.move_to(x, y)
        cr.show_text(text)

    def fit(self, width, height):
        active = [m for m in self.window.layout['monitors'] if m['enabled']]
        if not active:
            return
        left = min(m['x'] for m in active)
        top = min(m['y'] for m in active)
        right = max(m['x'] + core.dimensions(m)[0] for m in active)
        bottom = max(m['y'] + core.dimensions(m)[1] for m in active)
        self.factor = min((width - 100) / max(1, right - left), (height - 85) / max(1, bottom - top))
        self.origin_x = (width - (right - left) * self.factor) / 2 - left * self.factor
        self.origin_y = (height - (bottom - top) * self.factor) / 2 - top * self.factor - 6

    def draw(self, area, cr, width, height):
        cr.set_source_rgb(.075, .11, .16)
        cr.paint()
        cr.set_source_rgba(.6, .73, .88, .11)
        for x in range(20, width, 22):
            for y in range(20, height, 22):
                cr.rectangle(x, y, 1.3, 1.3)
        cr.fill()
        if self.dragging is None:
            self.fit(width, height)
        self.rects = {}
        for index, m in enumerate(self.window.layout['monitors']):
            if not m['enabled']:
                continue
            mw, mh = core.dimensions(m)
            x, y = self.origin_x + m['x'] * self.factor, self.origin_y + m['y'] * self.factor
            w, h = mw * self.factor, mh * self.factor
            self.rects[m['connector']] = (x, y, w, h)
            selected = m['connector'] == self.window.selected
            self.rounded(cr, x, y + 5, w, h)
            cr.set_source_rgba(0, 0, 0, .25)
            cr.fill()
            self.rounded(cr, x, y, w, h)
            cr.set_source_rgb(*((.13, .34, .43) if selected else (.16, .23, .32)))
            cr.fill_preserve()
            cr.set_line_width(2.5 if selected else 1.2)
            cr.set_source_rgb(*((.31, .84, .77) if selected else (.38, .46, .57)))
            cr.stroke()
            if w > 90 and h > 65:
                self.text(cr, f'{index + 1:02d}', x + 14, y + 29, 20, (.59, .93, .87) if selected else (.8, .86, .94), True)
                name = 'Portátil' if m.get('builtin') else m['connector']
                self.text(cr, name, x + 14, y + 51, 11, (.8, .87, .94))
                if h > 95:
                    self.text(cr, f"{m['width']} × {m['height']}", x + 14, y + h - 16, 10, (.65, .75, .84))
                if m['primary']:
                    # Draw the primary marker directly, independent of font glyphs.
                    import math
                    for point in range(10):
                        angle = -math.pi / 2 + point * math.pi / 5
                        radius = 8 if point % 2 == 0 else 3.6
                        px, py = x + w - 21 + math.cos(angle) * radius, y + 23 + math.sin(angle) * radius
                        cr.move_to(px, py) if point == 0 else cr.line_to(px, py)
                    cr.close_path()
                    cr.set_source_rgb(.97, .79, .39)
                    cr.fill()
            else:
                self.text(cr, str(index + 1), x + 8, y + 22, 14)
        self.text(cr, 'ARRASTA PARA MOVER  ·  CLICA PARA SELECIONAR', 20, height - 17, 9, (.55, .66, .77), True)

    def hit(self, x, y):
        for connector, (rx, ry, w, h) in reversed(list(self.rects.items())):
            if rx <= x <= rx + w and ry <= y <= ry + h:
                return connector
        return None

    def drag_begin(self, gesture, x, y):
        connector = self.hit(x, y)
        if not connector:
            return
        if self.window.layout.get('mirror_groups'):
            self.window.error('Monitores em espelho', 'Separa os monitores nas Definições do Ubuntu antes de editar esta disposição.')
            return
        self.window.selected = connector
        monitor = self.window.monitor()
        self.dragging = (connector, monitor['x'], monitor['y'])
        self.set_cursor_from_name('grabbing')
        self.window.refresh_inspector()
        self.queue_draw()

    def drag_update(self, gesture, dx, dy):
        if not self.dragging:
            return
        _, start_x, start_y = self.dragging
        monitor = self.window.monitor()
        monitor['x'], monitor['y'] = core.snap_position(monitor, self.window.layout['monitors'],
            start_x + dx / self.factor, start_y + dy / self.factor, threshold=22 / self.factor)
        self.window.dirty = True
        self.window.update_coordinates()
        self.queue_draw()

    def drag_end(self, gesture, dx, dy):
        if self.dragging:
            self.window.layout = core.normalize_layout(self.window.layout)
            self.dragging = None
            self.set_cursor_from_name('grab')
            self.window.refresh_inspector()
            self.queue_draw()


class SaveDialog(Gtk.Window):
    def __init__(self, parent):
        super().__init__(title='Guardar perfil', transient_for=parent, modal=True,
                         default_width=440, resizable=False)
        self.parent_window = parent
        profile = parent.store.find(parent.profile_id)
        self.shortcut = profile.get('shortcut', '') if profile else ''
        content = box(spacing=14, margin_top=24, margin_bottom=24, margin_start=24, margin_end=24)
        content.append(label('Guardar disposição e atalho', 'title-2'))
        content.append(label('Nome do perfil', 'heading'))
        self.name = Gtk.Entry(text=profile['name'] if profile else '', placeholder_text='Ex.: Monitor à direita, mais baixo')
        content.append(self.name)
        self.shortcut_button = button(accelerator_label(self.shortcut) if self.shortcut else 'Gravar combinação de teclas…', self.record)
        content.append(self.shortcut_button)
        content.append(label('O atalho funciona com a aplicação fechada e depois de reiniciar.', 'muted', wrap=True))
        self.problem = label('', 'error', wrap=True)
        content.append(self.problem)
        self.as_new = Gtk.CheckButton(label='Guardar como um novo perfil')
        self.as_new.set_visible(profile is not None)
        content.append(self.as_new)
        actions = box(Gtk.Orientation.HORIZONTAL, 8, halign=Gtk.Align.END)
        actions.append(button('Cancelar', lambda *_: self.close()))
        actions.append(button('Guardar perfil', self.save, 'suggested-action'))
        content.append(actions)
        self.set_child(content)

    def record(self, *_):
        ShortcutRecorder(self, self.shortcut, self.recorded).present()

    def recorded(self, value):
        self.shortcut = value
        self.shortcut_button.set_label(accelerator_label(value))
        self.problem.set_label('')

    def save(self, *_):
        name = self.name.get_text().strip()
        if not name or not self.shortcut:
            self.problem.set_label('Indica um nome e grava uma combinação de teclas.')
            return
        parent = self.parent_window
        existing = parent.store.find(parent.profile_id) if not self.as_new.get_active() else None
        try:
            if parent.bindings:
                conflict = parent.bindings.conflict(self.shortcut, existing.get('binding_path') if existing else None)
                if conflict:
                    raise ValueError(f'Esta combinação já está a ser usada por «{conflict}». Escolhe outra.')
            if parent.layout.get('mirror_groups'):
                raise ValueError('Separa os monitores em espelho antes de guardar um perfil editável.')
            layout = core.normalize_layout(parent.layout)
            core.validate_layout(layout)
            if parent.service:
                parent.service.apply(layout, verify=True)
            profile = copy.deepcopy(existing) if existing else {'id': uuid.uuid4().hex}
            profile.update(name=name, shortcut=self.shortcut, layout=layout, imported=False)
            original_data = copy.deepcopy(parent.store.data)
            previous_binding = None
            if existing:
                parent.store.data['profiles'] = [profile if p['id'] == profile['id'] else p for p in parent.store.profiles]
            else:
                parent.store.profiles.append(profile)
            if parent.bindings:
                profile.setdefault('binding_path', core.KEY_ROOT + 'monitores-atalhos-' + profile['id'] + '/')
                if profile['binding_path'] in parent.bindings.paths():
                    previous_binding = parent.bindings.read(profile['binding_path'])
            # Persist the target before enabling its shortcut; roll back on failure.
            try:
                parent.store.save()
            except Exception:
                parent.store.data = original_data
                raise
            try:
                if parent.bindings:
                    parent.bindings.register(profile)
            except Exception:
                parent.store.data = original_data
                parent.store.save()
                if previous_binding:
                    parent.bindings.write(profile['binding_path'], previous_binding)
                else:
                    parent.bindings.remove(profile)
                raise
            parent.profile_id = profile['id']
            parent.dirty = False
            parent.refresh_profiles()
            parent.toast('Perfil e atalho guardados no disco.')
            self.close()
        except Exception as error:
            self.problem.set_label(str(error))


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, application, demo=False):
        super().__init__(application=application, title='Monitores e Atalhos',
                         default_width=1080, default_height=840)
        self.demo = demo
        self.service = None if demo else core.DisplayService()
        if self.service:
            core.recover_stale_previews(self.service)
        self.bindings = None if demo else core.Bindings()
        self.layout = core.demo_layout() if demo else self.service.capture()
        self.demo_directory = tempfile.TemporaryDirectory(prefix='monitores-demo-') if demo else None
        self.store = core.ProfileStore(Path(self.demo_directory.name) / 'profiles.json' if demo else None)
        if demo:
            self.store.data['profiles'] = [dict(id='demo1', name='Monitor à direita', shortcut='<Control><Shift>2', layout=copy.deepcopy(self.layout))]
        else:
            self.store.import_existing(self.bindings, self.layout)
        self.profile_id = None
        self.selected = next((m['connector'] for m in self.layout['monitors'] if m['primary']), self.layout['monitors'][0]['connector'])
        self.dirty = False
        self.updating = False
        self.recovery_path = None
        self.confirmation = None
        self.set_size_request(840, 650)
        self.connect('close-request', self.close_requested)
        self.overlay = Adw.ToastOverlay()
        self.set_content(self.overlay)
        root = box(spacing=0)
        self.overlay.set_child(root)
        header = Adw.HeaderBar()
        header.set_title_widget(label('Monitores e Atalhos', 'heading'))
        header.pack_start(Gtk.Image.new_from_icon_name('video-display-symbolic'))
        about = Gtk.Button(icon_name='help-about-symbolic', tooltip_text='Sobre a aplicação')
        about.connect('clicked', self.about)
        header.pack_end(about)
        root.append(header)
        body = box(Gtk.Orientation.HORIZONTAL, 0, vexpand=True)
        root.append(body)
        sidebar = box(spacing=12, width_request=238)
        sidebar.add_css_class('sidebar')
        title = label('OS TEUS PERFIS', 'eyebrow', margin_start=20, margin_top=24)
        sidebar.append(title)
        self.profile_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE, vexpand=True)
        self.profile_list.add_css_class('navigation-sidebar')
        self.profile_list.connect('row-activated', self.profile_activated)
        sidebar_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        sidebar_scroll.set_child(self.profile_list)
        sidebar.append(sidebar_scroll)
        new = button('Novo perfil', self.new_profile, icon='list-add-symbolic')
        new.set_margin_start(16)
        new.set_margin_end(16)
        sidebar.append(new)
        note = label('Guardados no teu computador.\nDisponíveis após reiniciar.', 'footer-note', wrap=True,
                     margin_start=20, margin_end=16, margin_bottom=20)
        sidebar.append(note)
        body.append(sidebar)
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True, hexpand=True)
        body.append(scroll)
        main = box(spacing=16, margin_start=28, margin_end=28, margin_top=24, margin_bottom=24)
        scroll.set_child(main)
        intro = box(Gtk.Orientation.HORIZONTAL, 8)
        heading = box(spacing=4, hexpand=True)
        heading.append(label('DISPOSIÇÃO DOS MONITORES', 'eyebrow'))
        self.title_label = label('Organiza o teu espaço', 'main-title')
        heading.append(self.title_label)
        heading.append(label('Arrasta, ajusta e guarda com as tuas teclas.', 'muted'))
        intro.append(heading)
        main.append(intro)
        toolbar = box(Gtk.Orientation.HORIZONTAL, 8)
        self.state_label = label('Posição atual do Ubuntu', 'status-pill')
        toolbar.append(self.state_label)
        spacer = box(hexpand=True)
        toolbar.append(spacer)
        toolbar.append(button('Ler posição atual', self.read_current, icon='view-refresh-symbolic'))
        main.append(toolbar)
        self.canvas = LayoutCanvas(self)
        main.append(self.canvas)
        monitor_toolbar = box(Gtk.Orientation.HORIZONTAL, 8)
        monitor_toolbar.append(label('Monitor selecionado', 'heading'))
        self.monitor_dropdown = Gtk.DropDown(hexpand=True)
        self.monitor_dropdown.connect('notify::selected', self.monitor_selected)
        monitor_toolbar.append(self.monitor_dropdown)
        main.append(monitor_toolbar)
        inspector = box(spacing=12)
        inspector.add_css_class('panel')
        main.append(inspector)
        inspector_top = box(Gtk.Orientation.HORIZONTAL, 12)
        self.monitor_title = label('', 'title-3', hexpand=True)
        inspector_top.append(self.monitor_title)
        self.enabled = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.enabled.connect('notify::active', self.enabled_changed)
        inspector_top.append(label('Ligado', 'muted'))
        inspector_top.append(self.enabled)
        inspector.append(inspector_top)
        grid = Gtk.Grid(column_spacing=16, row_spacing=8, column_homogeneous=True)
        inspector.append(grid)
        grid.attach(label('Resolução e frequência', 'muted'), 0, 0, 2, 1)
        grid.attach(label('Rotação', 'muted'), 2, 0, 1, 1)
        grid.attach(label('Escala', 'muted'), 3, 0, 1, 1)
        self.resolution = Gtk.DropDown(hexpand=True)
        self.resolution.set_enable_search(True)
        self.resolution.connect('notify::selected', self.resolution_changed)
        grid.attach(self.resolution, 0, 1, 2, 1)
        self.rotation = Gtk.DropDown.new_from_strings(['Normal', '90°', '180°', '270°'])
        self.rotation.connect('notify::selected', self.rotation_changed)
        grid.attach(self.rotation, 2, 1, 1, 1)
        self.scale = Gtk.DropDown(hexpand=True)
        self.scale.connect('notify::selected', self.scale_changed)
        grid.attach(self.scale, 3, 1, 1, 1)
        controls = box(Gtk.Orientation.HORIZONTAL, 12)
        self.primary = Gtk.CheckButton(label='Monitor principal', hexpand=True)
        self.primary.connect('toggled', self.primary_changed)
        controls.append(self.primary)
        self.coordinates = label('', 'muted')
        controls.append(self.coordinates)
        inspector.append(controls)
        hint = label('As margens encaixam ao arrastar. As alterações só são aplicadas quando clicas em Aplicar.', 'footer-note', wrap=True)
        main.append(hint)
        actions = box(Gtk.Orientation.HORIZONTAL, 8)
        self.delete_button = button('Eliminar perfil', self.delete_profile)
        self.delete_button.add_css_class('destructive-action')
        self.delete_button.set_visible(False)
        actions.append(self.delete_button)
        actions.append(box(hexpand=True))
        actions.append(button('Aplicar', self.apply_layout))
        actions.append(button('Guardar perfil e atalho', self.save_dialog, 'suggested-action'))
        main.append(actions)
        main.append(label('Os atalhos funcionam mesmo com esta janela fechada.', 'footer-note'))
        self.refresh_profiles()
        self.refresh_inspector()

    def toast(self, text):
        self.overlay.add_toast(Adw.Toast.new(text))

    def error(self, title, body):
        dialog = Adw.MessageDialog.new(self, title, body)
        dialog.add_response('ok', 'Fechar')
        dialog.present()

    def about(self, *_):
        dialog = Adw.AboutWindow(transient_for=self, application_name='Monitores e Atalhos',
                                application_icon='video-display', version='1.0.0',
                                developer_name='Aplicação pessoal para Ubuntu',
                                comments='Editor de disposições com perfis e atalhos globais.\nUbuntu GNOME · Wayland e X11',
                                license_type=Gtk.License.MIT_X11)
        dialog.present()

    def monitor(self):
        return next(m for m in self.layout['monitors'] if m['connector'] == self.selected)

    def refresh_profiles(self):
        child = self.profile_list.get_first_child()
        while child:
            next_child = child.get_next_sibling()
            self.profile_list.remove(child)
            child = next_child
        for profile in self.store.profiles:
            row = Gtk.ListBoxRow()
            row.add_css_class('profile-row')
            row.profile_id = profile['id']
            content = box(spacing=4)
            name = label(profile['name'], 'heading', ellipsize=Pango.EllipsizeMode.END, max_width_chars=22)
            content.append(name)
            content.append(label(accelerator_label(profile.get('shortcut', '')), 'muted'))
            row.set_child(content)
            self.profile_list.append(row)
            if profile['id'] == self.profile_id:
                self.profile_list.select_row(row)
        self.delete_button.set_visible(self.profile_id is not None)

    def ask_discard(self, action):
        if not self.dirty:
            action()
            return
        dialog = Adw.MessageDialog.new(self, 'Descartar alterações?', 'Tens alterações à disposição que ainda não foram guardadas num perfil.')
        dialog.add_response('cancel', 'Continuar a editar')
        dialog.add_response('discard', 'Descartar')
        dialog.set_response_appearance('discard', Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response('cancel')
        dialog.connect('response', lambda d, response: action() if response == 'discard' else None)
        dialog.present()

    def profile_activated(self, listbox, row):
        def load():
            profile = self.store.find(row.profile_id)
            self.profile_id = profile['id']
            self.layout = copy.deepcopy(profile['layout'])
            # A profile that disables an output must still allow re-enabling it.
            live = core.demo_layout() if self.demo else self.service.capture()
            saved_connectors = {m['connector'] for m in self.layout['monitors']}
            for monitor in live['monitors']:
                if monitor['connector'] not in saved_connectors:
                    extra = copy.deepcopy(monitor)
                    extra.update(enabled=False, primary=False)
                    self.layout['monitors'].append(extra)
            self.selected = next(m['connector'] for m in self.layout['monitors'] if m['enabled'])
            self.dirty = False
            self.title_label.set_label(profile['name'])
            self.state_label.set_label('Perfil guardado · pré-visualização')
            self.refresh_inspector()
            self.delete_button.set_visible(True)
            self.canvas.queue_draw()
        self.ask_discard(load)

    def new_profile(self, *_):
        def create():
            self.profile_id = None
            self.profile_list.unselect_all()
            self.delete_button.set_visible(False)
            self.title_label.set_label('Novo perfil')
            self.read_current_unchecked()
        self.ask_discard(create)

    def read_current(self, *_):
        self.ask_discard(self.read_current_unchecked)

    def read_current_unchecked(self):
        try:
            self.layout = core.demo_layout() if self.demo else self.service.capture()
            self.selected = next(m['connector'] for m in self.layout['monitors'] if m['enabled'])
            self.dirty = self.profile_id is not None
            self.state_label.set_label('Posição atual do Ubuntu')
            self.refresh_inspector()
            self.canvas.queue_draw()
            self.toast('Posição atual carregada. Podes guardá-la neste perfil.')
        except Exception as error:
            self.error('Não foi possível ler os monitores', str(error))

    def refresh_inspector(self):
        self.updating = True
        monitors = self.layout['monitors']
        set_strings(self.monitor_dropdown, [f"{i + 1} · {m.get('name', m['connector'])}" for i, m in enumerate(monitors)])
        self.monitor_dropdown.set_selected(next(i for i, m in enumerate(monitors) if m['connector'] == self.selected))
        m = self.monitor()
        self.monitor_title.set_label(m.get('name', m['connector']))
        self.enabled.set_active(m['enabled'])
        self.rotation.set_selected(m['transform'] % 4)
        modes = m.get('modes', [])
        current_mode = next((v for v in modes if v['id'] == m['mode']), None)
        if current_mode is None:
            current_mode = dict(id=m['mode'], width=m['width'], height=m['height'], rate=m['rate'], scales=[m['scale']])
            modes = [current_mode] + modes
        self.available_modes = modes
        set_strings(self.resolution, [f"{v['width']} × {v['height']} · {v['rate']:.0f} Hz" for v in modes])
        self.resolution.set_selected(next(i for i, v in enumerate(modes) if v['id'] == m['mode']))
        self.available_scales = current_mode['scales']
        if not any(abs(s - m['scale']) < .01 for s in self.available_scales):
            self.available_scales = sorted(self.available_scales + [m['scale']])
        set_strings(self.scale, [f'{s * 100:g}%' for s in self.available_scales])
        self.scale.set_selected(next(i for i, s in enumerate(self.available_scales) if abs(s - m['scale']) < .01))
        self.primary.set_active(m['primary'])
        for control in (self.resolution, self.rotation, self.scale, self.primary):
            control.set_sensitive(m['enabled'])
        self.update_coordinates()
        self.updating = False

    def update_coordinates(self):
        m = self.monitor()
        self.coordinates.set_label(f"X {m['x']}   ·   Y {m['y']}")

    def monitor_selected(self, *_):
        if self.updating:
            return
        index = self.monitor_dropdown.get_selected()
        if index >= len(self.layout['monitors']):
            return
        self.selected = self.layout['monitors'][index]['connector']
        self.refresh_inspector()
        self.canvas.queue_draw()

    def changed(self):
        self.dirty = True
        self.state_label.set_label('Alterações por aplicar')
        self.layout = core.normalize_layout(self.layout)
        self.refresh_inspector()
        self.canvas.queue_draw()

    def adjust_after_resize(self, old):
        # Keep the previously touching edge when width, height or scale changes.
        m = self.monitor()
        ow, oh = core.dimensions(old)
        nw, nh = core.dimensions(m)
        for other in self.layout['monitors']:
            if other['connector'] == m['connector'] or not other['enabled']:
                continue
            bw, bh = core.dimensions(other)
            if abs(old['x'] + ow - other['x']) < .5:
                m['x'] = round(other['x'] - nw)
            if abs(old['y'] + oh - other['y']) < .5:
                m['y'] = round(other['y'] - nh)
        m['x'], m['y'] = core.snap_position(m, self.layout['monitors'], m['x'], m['y'], threshold=300)

    def rotation_changed(self, *_):
        if not self.updating:
            old = copy.deepcopy(self.monitor())
            self.monitor()['transform'] = self.rotation.get_selected()
            self.adjust_after_resize(old)
            self.changed()

    def scale_changed(self, *_):
        if not self.updating and self.scale.get_selected() < len(self.available_scales):
            old = copy.deepcopy(self.monitor())
            self.monitor()['scale'] = self.available_scales[self.scale.get_selected()]
            self.adjust_after_resize(old)
            self.changed()

    def resolution_changed(self, *_):
        if not self.updating and self.resolution.get_selected() < len(self.available_modes):
            old = copy.deepcopy(self.monitor())
            mode = self.available_modes[self.resolution.get_selected()]
            self.monitor().update(mode=mode['id'], width=mode['width'], height=mode['height'], rate=mode['rate'])
            if not any(abs(self.monitor()['scale'] - s) < .01 for s in mode['scales']):
                self.monitor()['scale'] = mode['scales'][0]
            self.adjust_after_resize(old)
            self.changed()

    def primary_changed(self, *_):
        if self.updating:
            return
        if self.primary.get_active():
            for m in self.layout['monitors']:
                m['primary'] = m['connector'] == self.selected
        self.changed()

    def enabled_changed(self, *_):
        if self.updating:
            return
        m = self.monitor()
        active = self.enabled.get_active()
        if not active and sum(v['enabled'] for v in self.layout['monitors']) == 1:
            self.toast('Mantém pelo menos um monitor ligado.')
            self.refresh_inspector()
            return
        m['enabled'] = active
        if active:
            others = [v for v in self.layout['monitors'] if v['enabled'] and v is not m]
            if others:
                m['x'] = round(max(v['x'] + core.dimensions(v)[0] for v in others))
                m['y'] = min(v['y'] for v in others)
        elif m['primary']:
            m['primary'] = False
            next(v for v in self.layout['monitors'] if v['enabled'])['primary'] = True
        self.changed()

    def save_dialog(self, *_):
        if self.recovery_path:
            self.toast('Confirma primeiro a disposição que estás a testar.')
            return
        SaveDialog(self).present()

    def apply_layout(self, *_):
        if self.recovery_path:
            return
        try:
            self.layout = core.normalize_layout(self.layout)
            core.validate_layout(self.layout)
            if self.demo:
                self.toast('Demonstração: os monitores reais não foram alterados.')
                return
            self.recovery_path = core.begin_preview(self.service, self.layout)
            self.deadline = time.monotonic() + 20
            self.confirmation = Adw.MessageDialog.new(self, 'Manter esta disposição?', '')
            self.confirmation.add_response('revert', 'Repor anterior')
            self.confirmation.add_response('keep', 'Manter disposição')
            self.confirmation.set_response_appearance('keep', Adw.ResponseAppearance.SUGGESTED)
            self.confirmation.set_default_response('keep')
            self.confirmation.set_close_response('revert')
            self.confirmation.connect('response', self.confirm_response)
            self.tick_confirmation()
            GLib.timeout_add_seconds(1, self.tick_confirmation)
            self.confirmation.present()
        except Exception as error:
            self.error('Não foi possível aplicar', str(error))

    def tick_confirmation(self):
        if not self.recovery_path:
            return False
        remaining = max(0, int(self.deadline - time.monotonic()) + 1)
        self.confirmation.set_body(f'Se não confirmares em {remaining} segundos, a disposição anterior será reposta automaticamente.')
        if remaining <= 0 or not self.recovery_path.exists():
            self.confirmation.response('revert')
            return False
        return True

    def confirm_response(self, dialog, response):
        path, self.recovery_path = self.recovery_path, None
        self.confirmation = None
        if not path:
            return
        try:
            if response == 'keep' and core.keep_preview(path):
                self.state_label.set_label('Disposição aplicada')
                self.toast('Disposição aplicada. Guarda um perfil para a recuperar por atalho.')
            else:
                core.revert_preview(self.service, path)
                self.toast('Disposição anterior reposta.')
        except Exception as error:
            self.error('Recuperação da disposição', str(error))

    def delete_profile(self, *_):
        profile = self.store.find(self.profile_id)
        if not profile:
            return
        message = 'O perfil e o respetivo atalho serão removidos.'
        if profile.get('original_binding'):
            message = 'Este perfil foi importado. Ao eliminá-lo, o comando original do teu atalho será reposto.'
        dialog = Adw.MessageDialog.new(self, f'Eliminar «{profile["name"]}»?', message)
        dialog.add_response('cancel', 'Cancelar')
        dialog.add_response('delete', 'Eliminar')
        dialog.set_response_appearance('delete', Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response('cancel')
        def response(dialog, choice):
            if choice != 'delete':
                return
            try:
                original_data = copy.deepcopy(self.store.data)
                path = profile.get('binding_path')
                original_binding = self.bindings.read(path) if self.bindings and path in self.bindings.paths() else None
                if self.bindings:
                    self.bindings.remove(profile)
                self.store.data['profiles'] = [p for p in self.store.profiles if p['id'] != profile['id']]
                try:
                    self.store.save()
                except Exception:
                    self.store.data = original_data
                    if original_binding:
                        self.bindings.write(path, original_binding)
                    raise
                self.profile_id = None
                self.dirty = False
                self.refresh_profiles()
                self.title_label.set_label('Organiza o teu espaço')
                self.toast('Perfil eliminado.')
            except Exception as error:
                self.error('Não foi possível eliminar', str(error))
        dialog.connect('response', response)
        dialog.present()

    def close_requested(self, *_):
        if self.recovery_path:
            self.confirmation.response('revert')
        if self.dirty:
            self.ask_discard(lambda: (setattr(self, 'dirty', False), self.close()))
            return True
        return False


class Application(Adw.Application):
    def __init__(self, demo=False):
        super().__init__(application_id='pt.local.MonitoresAtalhos', flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.demo = demo

    def do_activate(self):
        if self.get_active_window():
            self.get_active_window().present()
            return
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS.encode())
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        try:
            MainWindow(self, self.demo).present()
        except Exception as error:
            dialog = Adw.MessageDialog.new(None, 'Não foi possível abrir a aplicação', str(error))
            dialog.add_response('close', 'Fechar')
            dialog.connect('response', lambda *_: self.quit())
            self.add_window(dialog)
            dialog.present()


def main():
    parser = argparse.ArgumentParser(description='Editor de monitores e atalhos para Ubuntu GNOME')
    parser.add_argument('--apply', metavar='PERFIL', help='Aplicar um perfil sem abrir o editor')
    parser.add_argument('--verify', action='store_true', help='Verificar o perfil sem alterar os monitores')
    parser.add_argument('--watchdog', metavar='FICHEIRO', help=argparse.SUPPRESS)
    parser.add_argument('--demo', action='store_true', help='Abrir a demonstração sem alterar o sistema')
    args = parser.parse_args()
    try:
        if args.watchdog:
            core.watchdog(args.watchdog)
        elif args.apply:
            core.apply_profile(args.apply, args.verify)
        else:
            return Application(args.demo).run([sys.argv[0]])
    except Exception as error:
        print(str(error), file=sys.stderr)
        if not args.verify and not args.watchdog:
            core.notify('Monitores e Atalhos', str(error))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
