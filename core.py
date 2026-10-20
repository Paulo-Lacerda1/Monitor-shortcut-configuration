"""Persistence, GNOME shortcuts and Mutter display configuration."""
from __future__ import annotations

import copy
import fcntl
import json
import math
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager

from gi.repository import Gio, GLib

APP_DIR = Path(__file__).resolve().parent
CONFIG_DIR = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'monitores-atalhos'
CACHE_DIR = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')) / 'monitores-atalhos'
CONFIG_FILE = CONFIG_DIR / 'profiles.json'
KEY_ROOT = '/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/'
KEY_SCHEMA = 'org.gnome.settings-daemon.plugins.media-keys.custom-keybinding'
DEST = 'org.gnome.Mutter.DisplayConfig'
DBUS_PATH = '/org/gnome/Mutter/DisplayConfig'


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def dimensions(monitor):
    width, height = monitor['width'], monitor['height']
    if monitor.get('transform', 0) % 2:
        width, height = height, width
    return width / monitor['scale'], height / monitor['scale']


def normalize_layout(layout):
    result = copy.deepcopy(layout)
    active = [m for m in result['monitors'] if m['enabled']]
    if active:
        left = min(m['x'] for m in active)
        top = min(m['y'] for m in active)
        for m in active:
            m['x'] = round(m['x'] - left)
            m['y'] = round(m['y'] - top)
    return result


def validate_layout(layout):
    active = [m for m in layout['monitors'] if m['enabled']]
    if not active:
        raise ValueError('Mantém pelo menos um monitor ligado.')
    if sum(bool(m['primary']) for m in active) != 1:
        raise ValueError('Escolhe um único monitor principal.')
    graph = {i: set() for i in range(len(active))}
    for i, a in enumerate(active):
        aw, ah = dimensions(a)
        for j, b in enumerate(active[:i]):
            bw, bh = dimensions(b)
            overlap_x = min(a['x'] + aw, b['x'] + bw) - max(a['x'], b['x'])
            overlap_y = min(a['y'] + ah, b['y'] + bh) - max(a['y'], b['y'])
            if overlap_x > .5 and overlap_y > .5:
                raise ValueError('Os monitores estão sobrepostos. Arrasta-os até as margens se tocarem.')
            if (abs(overlap_x) < .5 and overlap_y > .5) or (abs(overlap_y) < .5 and overlap_x > .5):
                graph[i].add(j)
                graph[j].add(i)
    reached, pending = set(), [0]
    while pending:
        node = pending.pop()
        if node not in reached:
            reached.add(node)
            pending.extend(graph[node] - reached)
    if len(reached) != len(active):
        raise ValueError('Aproxima os monitores até as margens se tocarem, para o cursor poder passar entre eles.')


def snap_position(moving, others, x, y, threshold=100):
    """Prefer valid adjacent placements; never silently retain an overlapping drop."""
    width, height = dimensions(moving)
    candidates = []
    for other in others:
        if not other['enabled'] or other['connector'] == moving['connector']:
            continue
        ow, oh = dimensions(other)
        ox, oy = other['x'], other['y']
        ys = [y, oy, oy + oh - height, oy + (oh - height) / 2]
        xs = [x, ox, ox + ow - width, ox + (ow - width) / 2]
        for edge_x in (ox - width, ox + ow):
            for cy in ys:
                if min(cy + height, oy + oh) - max(cy, oy) > 1:
                    candidates.append((edge_x, cy))
        for edge_y in (oy - height, oy + oh):
            for cx in xs:
                if min(cx + width, ox + ow) - max(cx, ox) > 1:
                    candidates.append((cx, edge_y))
    candidates.sort(key=lambda p: math.hypot(p[0] - x, p[1] - y))
    for cx, cy in candidates:
        if math.hypot(cx - x, cy - y) > threshold:
            break
        if all(not o['enabled'] or o['connector'] == moving['connector'] or
               min(cx + width, o['x'] + dimensions(o)[0]) - max(cx, o['x']) <= .5 or
               min(cy + height, o['y'] + dimensions(o)[1]) - max(cy, o['y']) <= .5
               for o in others):
            return round(cx), round(cy)
    return round(x), round(y)


class DisplayService:
    def __init__(self):
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)

    def call(self, method, args=None):
        return self.bus.call_sync(DEST, DBUS_PATH, DEST, method, args, None,
                                  Gio.DBusCallFlags.NONE, 10000, None).unpack()

    def state(self):
        return self.call('GetCurrentState')

    def capture(self):
        state = self.state()
        logical_by_connector = {spec[0]: item for item in state[2] for spec in item[5]}
        monitors = []
        for spec, modes, properties in state[1]:
            connector, vendor, product, serial = spec
            logical = logical_by_connector.get(connector)
            current = next((m for m in modes if m[6].get('is-current')), None)
            mode = current or next((m for m in modes if m[6].get('is-preferred')), modes[0])
            monitors.append({
                'connector': connector, 'vendor': vendor, 'product': product, 'serial': serial,
                'name': properties.get('display-name', product),
                'builtin': properties.get('is-builtin', connector.startswith('eDP')),
                'mode': mode[0], 'width': mode[1], 'height': mode[2], 'rate': mode[3],
                'x': logical[0] if logical else 0, 'y': logical[1] if logical else 0,
                'scale': logical[2] if logical else 1.0,
                'transform': logical[3] if logical else 0,
                'primary': logical[4] if logical else False,
                'enabled': logical is not None,
                'underscanning': properties.get('is-underscanning', False),
                'modes': [{'id': m[0], 'width': m[1], 'height': m[2], 'rate': m[3],
                           'scales': list(m[5])} for m in modes],
            })
        # Preserve mirrored configurations when capturing/restoring, but edit them
        # only after explicitly separating monitors in the editor.
        groups = [[s[0] for s in logical[5]] for logical in state[2] if len(logical[5]) > 1]
        return {'layout_mode': state[3].get('layout-mode', 1), 'monitors': monitors, 'mirror_groups': groups}

    def apply(self, layout, verify=False, trusted_restore=False):
        layout = normalize_layout(layout)
        if not trusted_restore:
            if layout.get('mirror_groups'):
                raise ValueError('Esta disposição está em espelho. Separa os monitores nas Definições do Ubuntu antes de a editar.')
            validate_layout(layout)
        state = self.state()
        available = {m[0][0]: m for m in state[1]}
        logical, handled = [], set()
        for saved in layout['monitors']:
            connector = saved['connector']
            if not saved['enabled'] or connector in handled:
                continue
            group = next((g for g in layout.get('mirror_groups', []) if connector in g), [connector])
            outputs = []
            for member in group:
                m = next(m for m in layout['monitors'] if m['connector'] == member)
                if member not in available:
                    raise ValueError(f'O monitor {member} não está ligado. Liga-o e tenta novamente.')
                hardware = available[member]
                # Connector names may change after reconnect; never apply to a different model.
                if m.get('vendor') and m.get('product') and tuple(hardware[0][1:3]) != (m['vendor'], m['product']):
                    raise ValueError(f'O monitor ligado em {member} é diferente do guardado neste perfil.')
                mode = next((v for v in hardware[1] if v[0] == m['mode']), None)
                if mode is None:
                    candidates = [v for v in hardware[1] if v[1:3] == (m['width'], m['height']) and abs(v[3] - m['rate']) < 1]
                    mode = candidates[0] if candidates else None
                if mode is None:
                    raise ValueError(f'A resolução ou frequência guardada já não está disponível em {member}.')
                if not any(abs(m['scale'] - s) < .01 for s in mode[5]):
                    raise ValueError(f'A escala guardada não está disponível em {member}.')
                props = {}
                if hardware[2].get('is-underscanning') is not None:
                    props['underscanning'] = GLib.Variant('b', m.get('underscanning', False))
                outputs.append((member, mode[0], props))
                handled.add(member)
            logical.append((saved['x'], saved['y'], saved['scale'], saved['transform'], saved['primary'], outputs))
        args = GLib.Variant('(uua(iiduba(ssa{sv}))a{sv})',
                            (state[0], 0 if verify else 1, logical,
                             {'layout-mode': GLib.Variant('u', layout.get('layout_mode', 1))}))
        self.call('ApplyMonitorsConfig', args)


def read_legacy(command, metadata):
    """Read known local monitor scripts as data; never execute imported code."""
    tokens = shlex.split(command)
    if len(tokens) != 1:
        return None
    path = Path(tokens[0]).expanduser()
    if path.parent != Path.home() / '.local/bin' or not re.fullmatch(r'monitor-\d+', path.name):
        return None
    source = path.read_text()
    match = re.search(r"PROFILE = json.loads\(r'''(.*?)'''\)", source, re.S)
    if match:
        parsed = json.loads(match[1])
        entries = [(m['x'], m['y'], m['scale'], m['transform'], m['primary'],
                    [(o['connector'], o['mode'], {}) for o in m['outputs']]) for m in parsed['logical_monitors']]
        layout_mode = parsed['layout_mode']
    else:
        match = re.search(r'"(\[\(int32 .*?\])"', source)
        if not match:
            return None
        entries = GLib.Variant.parse(GLib.VariantType.new('a(iiduba(ssa{sv}))'), match[1], None, None).unpack()
        layout_mode = 1
    monitors = []
    for x, y, scale, transform, primary, outputs in entries:
        for connector, mode, properties in outputs:
            size = re.fullmatch(r'(\d+)x(\d+)@([\d.]+)', mode)
            if not size:
                return None
            monitor = copy.deepcopy(metadata.get(connector, {}))
            monitor.update(connector=connector, mode=mode, width=int(size[1]), height=int(size[2]),
                           rate=float(size[3]), x=x, y=y, scale=scale, transform=transform,
                           primary=primary, enabled=True)
            monitors.append(monitor)
    return {'layout_mode': layout_mode, 'monitors': monitors, 'mirror_groups': []}


class ProfileStore:
    def __init__(self, path=None):
        self.path = Path(path or CONFIG_FILE)
        self.data = {'version': 1, 'profiles': []}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
                if self.data.get('version') != 1 or not isinstance(self.data.get('profiles'), list):
                    raise ValueError('Formato desconhecido')
            except (ValueError, KeyError) as error:
                raise ValueError(f'Não foi possível ler {self.path}. O ficheiro foi preservado: {error}') from error

    @property
    def profiles(self):
        return self.data['profiles']

    def save(self):
        atomic_json(self.path, self.data)

    def find(self, identifier):
        return next((p for p in self.profiles if p['id'] == identifier), None)

    def import_existing(self, bindings, layout):
        metadata = {m['connector']: m for m in layout['monitors']}
        known = {p.get('binding_path') for p in self.profiles}
        changed = False
        for path in bindings.paths():
            if path in known:
                continue
            original = bindings.read(path)
            try:
                parsed = read_legacy(original['command'], metadata)
            except (OSError, ValueError, GLib.Error):
                continue
            if parsed:
                self.profiles.append({'id': uuid.uuid4().hex, 'name': original['name'],
                                      'shortcut': original['binding'], 'binding_path': path,
                                      'original_binding': original, 'layout': parsed, 'imported': True})
                changed = True
        if changed:
            self.save()


class Bindings:
    def __init__(self):
        self.root = Gio.Settings.new('org.gnome.settings-daemon.plugins.media-keys')

    def paths(self):
        return self.root.get_strv('custom-keybindings')

    def settings(self, path):
        return Gio.Settings.new_with_path(KEY_SCHEMA, path)

    def read(self, path):
        settings = self.settings(path)
        return {key: settings.get_string(key) for key in ('name', 'command', 'binding')}

    def write(self, path, values):
        settings = self.settings(path)
        settings.delay()
        for key, value in values.items():
            if not settings.set_string(key, value):
                settings.revert()
                raise ValueError('O Ubuntu não permitiu guardar o atalho.')
        settings.apply()
        paths = self.paths()
        if path not in paths:
            if not self.root.set_strv('custom-keybindings', paths + [path]):
                raise ValueError('O Ubuntu não permitiu ativar o atalho.')
        Gio.Settings.sync()

    def register(self, profile):
        path = profile.setdefault('binding_path', KEY_ROOT + 'monitores-atalhos-' + profile['id'] + '/')
        command = shlex.join(['/usr/bin/python3', str(APP_DIR / 'app.py'), '--apply', profile['id']])
        self.write(path, {'name': profile['name'], 'command': command, 'binding': profile['shortcut']})

    def remove(self, profile):
        path = profile.get('binding_path')
        if not path:
            return
        if profile.get('original_binding'):
            self.write(path, profile['original_binding'])
        else:
            if not self.root.set_strv('custom-keybindings', [p for p in self.paths() if p != path]):
                raise ValueError('O Ubuntu não permitiu remover o atalho.')
            settings = self.settings(path)
            for key in ('name', 'command', 'binding'):
                settings.reset(key)
            Gio.Settings.sync()

    def conflict(self, shortcut, exclude_path=None):
        import gi
        gi.require_version('Gtk', '4.0')
        from gi.repository import Gtk

        def canonical(value):
            valid, key, mods = Gtk.accelerator_parse(value)
            return (key, int(mods)) if valid else None

        wanted = canonical(shortcut)
        for path in self.paths():
            item = self.read(path)
            if path != exclude_path and canonical(item['binding']) == wanted:
                return item['name'] or 'outro atalho personalizado'
        source = Gio.SettingsSchemaSource.get_default()
        for schema in ('org.gnome.desktop.wm.keybindings', 'org.gnome.mutter.keybindings',
                       'org.gnome.mutter.wayland.keybindings', 'org.gnome.shell.keybindings',
                       'org.gnome.settings-daemon.plugins.media-keys'):
            found = source.lookup(schema, True)
            if not found:
                continue
            settings = Gio.Settings.new(schema)
            for key in found.list_keys():
                value = settings.get_value(key)
                if value.get_type_string() == 'as' and any(canonical(v) == wanted for v in value.unpack()):
                    return 'atalho do sistema: ' + key
        return None


@contextmanager
def recovery_lock():
    CACHE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (CACHE_DIR / 'recovery.lock').open('w') as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        yield


def begin_preview(service, layout, seconds=20):
    # Avoid racing two independently launched editors.
    with recovery_lock():
        if list(CACHE_DIR.glob('recovery-*.json')):
            raise ValueError('Já existe uma disposição em confirmação. Confirma-a ou aguarda que seja reposta.')
        previous = service.capture()
        service.apply(layout, verify=True)
        path = CACHE_DIR / ('recovery-' + uuid.uuid4().hex + '.json')
        atomic_json(path, {'layout': previous, 'deadline': time.time() + seconds,
                           'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip()})
        try:
            subprocess.Popen(['/usr/bin/python3', str(APP_DIR / 'app.py'), '--watchdog', str(path)],
                             start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            service.apply(layout)
        except Exception:
            # Roll back even if the apply succeeded but the response was lost.
            try:
                service.apply(previous, trusted_restore=True)
                path.unlink(missing_ok=True)
            except Exception:
                pass  # Watchdog retains the recovery record and retries.
            raise
        return path


def keep_preview(path):
    with recovery_lock():
        if not Path(path).exists():
            return False
        Path(path).unlink()
        return True


def revert_preview(service, path):
    with recovery_lock():
        if not Path(path).exists():
            return
        data = json.loads(Path(path).read_text())
        service.apply(data['layout'], trusted_restore=True)
        Path(path).unlink(missing_ok=True)


def watchdog(path):
    path = Path(path)
    # Only accept application-owned recovery records, including from the CLI.
    if path.parent.resolve() != CACHE_DIR.resolve() or not re.fullmatch(r'recovery-[a-f0-9]{32}\.json', path.name):
        raise ValueError('Ficheiro de recuperação inválido.')
    if not path.exists():
        return
    data = json.loads(path.read_text())
    while path.exists() and time.time() < data['deadline']:
        time.sleep(.25)
    if path.exists():
        for attempt in range(3):
            try:
                revert_preview(DisplayService(), path)
                return
            except Exception:
                time.sleep(1)
        # An unplugged monitor can make restoration impossible. Preserve the
        # snapshot for diagnosis without permanently blocking future shortcuts.
        with recovery_lock():
            if path.exists():
                path.rename(path.with_suffix('.failed'))
        notify('Não foi possível repor os monitores', 'A ligação dos monitores mudou. Abre as Definições do Ubuntu para ajustar a disposição.')


def recover_stale_previews(service):
    """Recover records left behind if the machine/session stopped the watchdog."""
    if not CACHE_DIR.exists():
        return
    with recovery_lock():
        for path in CACHE_DIR.glob('recovery-*.json'):
            try:
                data = json.loads(path.read_text())
                if time.time() <= data['deadline'] + 10:
                    continue
                # After a restart, use the configuration chosen by this session;
                # never restore coordinates from an earlier boot.
                current_boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
                if data.get('boot_id') == current_boot:
                    service.apply(data['layout'], trusted_restore=True)
                path.unlink(missing_ok=True)
            except Exception:
                path.rename(path.with_suffix('.failed'))


def notify(title, message):
    subprocess.run(['notify-send', '--app-name=Monitores e Atalhos', title, message], check=False)


def apply_profile(identifier, verify=False):
    store = ProfileStore()
    profile = store.find(identifier)
    if profile is None:
        raise ValueError('Este perfil já não existe. Abre Monitores e Atalhos para o configurar.')
    service = DisplayService()
    if not verify:
        recover_stale_previews(service)
    with recovery_lock():
        if list(CACHE_DIR.glob('recovery-*.json')):
            raise ValueError('Confirma ou reverte primeiro a disposição que estás a testar.')
        service.apply(profile['layout'], verify=verify)
    if not verify:
        notify('Disposição aplicada', profile['name'])


def demo_layout():
    result = {'layout_mode': 1, 'mirror_groups': [], 'monitors': []}
    for connector, name, width, height, x, y, primary in (
        ('eDP-1', 'Ecrã do portátil', 1920, 1080, 0, 360, True),
        ('HDMI-1', 'AOC Q27B36X', 2560, 1440, 1920, 0, False)):
        mode = f'{width}x{height}@144.000'
        result['monitors'].append(dict(connector=connector, name=name, width=width, height=height,
            x=x, y=y, primary=primary, enabled=True, scale=1., transform=0, rate=144., mode=mode,
            builtin=connector.startswith('eDP'), modes=[dict(id=mode, width=width, height=height,
            rate=144., scales=[1., 1.25, 1.5, 2.])]))
    return result
