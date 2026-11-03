"""Hardware brightness: GNOME for the panel, libddcutil for external displays."""
from __future__ import annotations

import ctypes as C
import ctypes.util
import json
from pathlib import Path
import re
import subprocess
import sys
import time

from gi.repository import Gio, GLib

POWER = 'org.gnome.SettingsDaemon.Power'
POWER_PATH = '/org/gnome/SettingsDaemon/Power'
SCREEN = POWER + '.Screen'
DRM_DIR = Path('/sys/class/drm')


def percentage(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100:
        raise ValueError('O brilho tem de estar entre 0 e 100%.')
    return round(value)


class BrightnessService:
    def __init__(self, bus):
        self.bus = bus

    def panel_call(self, method, args):
        return self.bus.call_sync(POWER, POWER_PATH, 'org.freedesktop.DBus.Properties',
                                  method, args, None, Gio.DBusCallFlags.NONE, 5000, None).unpack()

    def read(self, monitor):
        if monitor.get('builtin'):
            value = self.panel_call('Get', GLib.Variant('(ss)', (SCREEN, 'Brightness')))[0]
            if value < 0:
                raise ValueError('O GNOME não disponibiliza o brilho deste ecrã.')
            return percentage(value)
        return self.external(monitor)

    def set(self, monitor, value):
        value = percentage(value)
        if monitor.get('builtin'):
            self.panel_call('Set', GLib.Variant('(ssv)', (SCREEN, 'Brightness', GLib.Variant('i', value))))
            # GNOME acknowledges Set before the backlight/property catches up.
            deadline = time.monotonic() + 3
            while True:
                actual = self.read(monitor)
                if abs(actual - value) <= 1:
                    return actual
                if time.monotonic() >= deadline:
                    raise ValueError('O GNOME recebeu o pedido mas não confirmou a alteração do brilho.')
                time.sleep(.05)
        return self.external(monitor, value)

    def external(self, monitor, value=None):
        # Mutter uses HDMI-1 where DRM uses HDMI-A-1. Require an unambiguous
        # connected output, then select DDC by its complete EDID, never display 1.
        connector = monitor['connector']
        aliases = {connector, re.sub(r'^HDMI-(\d+)$', r'HDMI-A-\1', connector)}
        candidates = []
        for path in DRM_DIR.glob('card*-*'):
            if re.sub(r'^card\d+-', '', path.name) not in aliases:
                continue
            try:
                if (path / 'status').read_text().strip() == 'connected':
                    candidates.append((path / 'edid').read_bytes()[:128])
            except OSError:
                continue
        if len(candidates) != 1 or len(candidates[0]) != 128:
            raise ValueError(f'Não foi possível identificar o monitor {connector} para controlar o brilho.')
        command = [sys.executable, str(Path(__file__).resolve()), candidates[0].hex()]
        if value is not None:
            command.append(str(percentage(value)))
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=20)
        except subprocess.TimeoutExpired as error:
            raise ValueError(f'O monitor {connector} não respondeu ao pedido de brilho.') from error
        try:
            data = json.loads(result.stdout)
        except ValueError as error:
            raise ValueError('Não foi possível comunicar com o monitor por DDC/CI.') from error
        if result.returncode or 'error' in data:
            raise ValueError(data.get('error', 'Não foi possível alterar o brilho.'))
        return percentage(data['brightness'])


class VcpValue(C.Structure):
    _fields_ = [(name, C.c_ubyte) for name in ('mh', 'ml', 'sh', 'sl')]


def ddc_brightness(edid, value=None):
    """Run in a short-lived worker so discovery is fresh after hotplug."""
    library = ctypes.util.find_library('ddcutil')
    if not library:
        raise ValueError('Instala libddcutil para controlar o brilho do monitor externo (sudo apt install libddcutil4).')
    lib = C.CDLL(library)
    pointer = C.c_void_p
    signatures = {
        'ddca_create_edid_display_identifier': ([C.POINTER(C.c_ubyte), C.POINTER(pointer)], C.c_int),
        'ddca_get_display_ref': ([pointer, C.POINTER(pointer)], C.c_int),
        'ddca_open_display2': ([pointer, C.c_bool, C.POINTER(pointer)], C.c_int),
        'ddca_close_display': ([pointer], C.c_int),
        'ddca_free_display_identifier': ([pointer], C.c_int),
        'ddca_get_non_table_vcp_value': ([pointer, C.c_ubyte, C.POINTER(VcpValue)], C.c_int),
        'ddca_set_non_table_vcp_value': ([pointer, C.c_ubyte, C.c_ubyte, C.c_ubyte], C.c_int),
        'ddca_rc_desc': ([C.c_int], C.c_char_p),
        'ddca_set_fout': ([pointer], None),
        'ddca_set_ferr': ([pointer], None),
    }
    for name, (args, result) in signatures.items():
        function = getattr(lib, name)
        function.argtypes, function.restype = args, result
    lib.ddca_set_fout(None)
    lib.ddca_set_ferr(None)

    def check(status):
        if status:
            detail = lib.ddca_rc_desc(status).decode(errors='replace')
            raise ValueError('Não foi possível controlar o brilho por DDC/CI: ' + detail +
                             '. Verifica se DDC/CI está ativo no menu do monitor e se tens acesso a /dev/i2c-*.')

    identifier, reference, handle = pointer(), pointer(), pointer()
    raw = (C.c_ubyte * 128).from_buffer_copy(edid)
    try:
        check(lib.ddca_create_edid_display_identifier(raw, C.byref(identifier)))
        check(lib.ddca_get_display_ref(identifier, C.byref(reference)))
        check(lib.ddca_open_display2(reference, False, C.byref(handle)))
        reading = VcpValue()
        check(lib.ddca_get_non_table_vcp_value(handle, 0x10, C.byref(reading)))
        maximum = reading.mh * 256 + reading.ml
        if maximum <= 0:
            raise ValueError('O monitor não disponibiliza uma escala de brilho válida.')
        if value is not None:
            target = round(percentage(value) * maximum / 100)
            check(lib.ddca_set_non_table_vcp_value(handle, 0x10, target >> 8, target & 255))
            check(lib.ddca_get_non_table_vcp_value(handle, 0x10, C.byref(reading)))
            actual = reading.sh * 256 + reading.sl
            if abs(actual - target) > 1:
                raise ValueError('O monitor recebeu o pedido mas não alterou o brilho. Verifica os modos HDR, DCR ou brilho automático no menu do monitor.')
        return percentage((reading.sh * 256 + reading.sl) * 100 / maximum)
    finally:
        if handle:
            lib.ddca_close_display(handle)
        if identifier:
            lib.ddca_free_display_identifier(identifier)


if __name__ == '__main__':
    try:
        print(json.dumps({'brightness': ddc_brightness(bytes.fromhex(sys.argv[1]),
                            int(sys.argv[2]) if len(sys.argv) > 2 else None)}))
    except Exception as error:
        print(json.dumps({'error': str(error)}))
        sys.exit(1)
