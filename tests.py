#!/usr/bin/python3
"""Run without a session bus or real monitor changes."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import subprocess
import brightness

os.environ['GSETTINGS_BACKEND'] = 'memory'
import core


class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.layout = core.demo_layout()

    def test_actual_offset_is_valid(self):
        core.validate_layout(self.layout)
        self.assertEqual(self.layout['monitors'][0]['y'], 360)

    def test_rotation_and_scale_change_logical_size(self):
        monitor = self.layout['monitors'][0]
        monitor.update(scale=1.5, transform=1)
        self.assertEqual(core.dimensions(monitor), (720, 1280))

    def test_overlapping_monitors_rejected(self):
        self.layout['monitors'][1]['x'] = 1800
        with self.assertRaisesRegex(ValueError, 'sobrepostos'):
            core.validate_layout(self.layout)

    def test_gap_rejected(self):
        self.layout['monitors'][1]['x'] = 2000
        with self.assertRaisesRegex(ValueError, 'margens'):
            core.validate_layout(self.layout)

    def test_corner_only_contact_rejected(self):
        self.layout['monitors'][0]['y'] = 1440
        with self.assertRaises(ValueError):
            core.validate_layout(self.layout)

    def test_last_monitor_cannot_be_disabled(self):
        for monitor in self.layout['monitors']:
            monitor['enabled'] = False
        with self.assertRaisesRegex(ValueError, 'pelo menos'):
            core.validate_layout(self.layout)

    def test_snap_corrects_overlap_and_aligns_edge(self):
        moving, other = self.layout['monitors']
        x, y = core.snap_position(other, self.layout['monitors'], 1890, 0, threshold=100)
        self.assertEqual(x, 1920)
        other.update(x=x, y=y)
        core.validate_layout(self.layout)

    def test_normalize_preserves_relative_position(self):
        for m in self.layout['monitors']:
            m['x'] -= 500
            m['y'] -= 100
        original = copy.deepcopy(self.layout)
        normalized = core.normalize_layout(self.layout)
        self.assertEqual(normalized['monitors'][0]['x'], 0)
        self.assertEqual(normalized['monitors'][1]['x'], 1920)
        self.assertEqual(normalized['monitors'][0]['y'], 360)
        self.assertEqual(self.layout, original)


class PersistenceTests(unittest.TestCase):
    def test_roundtrip_survives_new_process_store(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'profiles.json'
            store = core.ProfileStore(path)
            profile = dict(id='abc', name='À direita', shortcut='<Control><Shift>2', layout=core.demo_layout())
            store.profiles.append(profile)
            store.save()
            self.assertEqual(core.ProfileStore(path).find('abc'), profile)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_invalid_file_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'profiles.json'
            path.write_text('broken file')
            with self.assertRaises(ValueError):
                core.ProfileStore(path)
            self.assertEqual(path.read_text(), 'broken file')

    def test_failed_replace_preserves_previous_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'profiles.json'
            core.atomic_json(path, {'old': True})
            with patch.object(core.os, 'replace', side_effect=OSError('disk failure')):
                with self.assertRaises(OSError):
                    core.atomic_json(path, {'new': True})
            self.assertEqual(json.loads(path.read_text()), {'old': True})
            self.assertEqual(len(list(path.parent.iterdir())), 1)


class BindingTests(unittest.TestCase):
    def setUp(self):
        self.bindings = core.Bindings()
        for path in self.bindings.paths():
            settings = self.bindings.settings(path)
            for key in ('name', 'command', 'binding'):
                settings.reset(key)
        self.bindings.root.set_strv('custom-keybindings', [])

    def test_register_preserves_unrelated_shortcuts(self):
        unrelated = core.KEY_ROOT + 'unrelated/'
        original = dict(name='Outro', command='true', binding='<Control><Alt>z')
        self.bindings.write(unrelated, original)
        profile = dict(id='123', name='Direita', shortcut='<Control><Shift>2')
        self.bindings.register(profile)
        self.assertEqual(self.bindings.read(unrelated), original)
        self.assertIn('--apply 123', self.bindings.read(profile['binding_path'])['command'])
        self.assertEqual(len(self.bindings.paths()), 2)
        self.bindings.remove(profile)
        self.assertEqual(self.bindings.paths(), [unrelated])

    def test_imported_binding_is_restored_on_removal(self):
        path = core.KEY_ROOT + 'monitor2/'
        original = dict(name='Monitor à direita', command='/home/test/.local/bin/monitor-2', binding='<Control><Shift>2')
        self.bindings.write(path, original)
        profile = dict(id='123', name='Novo nome', shortcut='<Control><Alt>9', binding_path=path, original_binding=original)
        self.bindings.register(profile)
        self.bindings.remove(profile)
        self.assertEqual(self.bindings.read(path), original)

    def test_duplicate_shortcut_rejected_but_same_profile_allowed(self):
        path = core.KEY_ROOT + 'conflict/'
        self.bindings.write(path, dict(name='Ocupado', command='true', binding='<Control><Shift>2'))
        self.assertEqual(self.bindings.conflict('<Shift><Control>2'), 'Ocupado')
        self.assertNotEqual(self.bindings.conflict('<Shift><Control>2', path), 'Ocupado')

    def test_system_shortcut_detected(self):
        from gi.repository import Gio
        system = Gio.Settings.new('org.gnome.desktop.wm.keybindings')
        system.set_strv('close', ['<Alt>F4'])
        self.assertIn('close', self.bindings.conflict('<Alt>F4'))


class RecoveryTests(unittest.TestCase):
    def test_restore_applies_saved_layout_and_removes_record(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(core, 'CACHE_DIR', Path(directory)):
            path = Path(directory) / 'recovery-test.json'
            original = core.demo_layout()
            core.atomic_json(path, {'layout': original})
            service = unittest.mock.Mock()
            core.revert_preview(service, path)
            service.apply.assert_called_once_with(original, trusted_restore=True)
            self.assertFalse(path.exists())

    def test_keep_removes_record_without_applying(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(core, 'CACHE_DIR', Path(directory)):
            path = Path(directory) / 'recovery-test.json'
            path.write_text('{}')
            self.assertTrue(core.keep_preview(path))
            self.assertFalse(core.keep_preview(path))

    def test_failed_restore_keeps_record_for_retry(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(core, 'CACHE_DIR', Path(directory)):
            path = Path(directory) / 'recovery-test.json'
            core.atomic_json(path, {'layout': core.demo_layout()})
            service = unittest.mock.Mock()
            service.apply.side_effect = RuntimeError('disconnected')
            with self.assertRaises(RuntimeError):
                core.revert_preview(service, path)
            self.assertTrue(path.exists())


def hardware_state(layout):
    hardware, logical = [], []
    for monitor in layout['monitors']:
        spec = (monitor['connector'], monitor.get('vendor', ''), monitor.get('product', ''), monitor.get('serial', ''))
        mode = (monitor['mode'], monitor['width'], monitor['height'], monitor['rate'], 1., [1.], {'is-current': True})
        hardware.append((spec, [mode], {'is-builtin': monitor.get('builtin', False)}))
        if monitor['enabled']:
            logical.append((monitor['x'], monitor['y'], 1., 0, monitor['primary'], [spec], {}))
    return (1, hardware, logical, {})


class BrightnessTests(unittest.TestCase):
    def test_percentage_rejects_invalid_values(self):
        for value in (-1, 101, True, '50', None, float('nan'), float('inf')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                brightness.percentage(value)

    def test_panel_set_waits_for_gnome_to_report_new_value(self):
        service = brightness.BrightnessService(Mock())
        service.panel_call = Mock()
        service.read = Mock(side_effect=[95, 92])
        with patch.object(brightness.time, 'sleep'):
            self.assertEqual(service.set({'builtin': True}, 92), 92)
        args = service.panel_call.call_args.args
        self.assertEqual(args[0], 'Set')
        self.assertEqual(args[1].unpack(), (brightness.SCREEN, 'Brightness', 92))

    def test_panel_reports_failure_if_brightness_never_changes(self):
        service = brightness.BrightnessService(Mock())
        service.panel_call = Mock()
        service.read = Mock(return_value=95)
        with patch.object(brightness.time, 'monotonic', side_effect=[0, 4]):
            with self.assertRaisesRegex(ValueError, 'não confirmou'):
                service.set({'builtin': True}, 50)

    def test_unsupported_panel_is_reported(self):
        service = brightness.BrightnessService(Mock())
        service.panel_call = Mock(return_value=(-1,))
        with self.assertRaisesRegex(ValueError, 'não disponibiliza'):
            service.read({'builtin': True})

    def test_external_uses_selected_connector_edid_and_absolute_percent(self):
        with tempfile.TemporaryDirectory() as directory:
            drm = Path(directory)
            edid = bytes(range(128))
            for connector, data in [('card2-HDMI-A-1', edid), ('card2-DP-1', bytes(128))]:
                path = drm / connector
                path.mkdir()
                (path / 'edid').write_bytes(data)
                (path / 'status').write_text('connected\n')
            with patch.object(brightness, 'DRM_DIR', drm), patch.object(brightness.subprocess, 'run') as run:
                run.return_value = subprocess.CompletedProcess([], 0, '{"brightness": 37}', '')
                self.assertEqual(brightness.BrightnessService(None).set({'connector': 'HDMI-1'}, 37), 37)
                command = run.call_args.args[0]
                self.assertEqual(command[-2:], [edid.hex(), '37'])

    def test_external_failure_is_not_reported_as_success(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'card2-HDMI-A-1'
            path.mkdir()
            (path / 'edid').write_bytes(bytes(128))
            (path / 'status').write_text('connected')
            with patch.object(brightness, 'DRM_DIR', Path(directory)), patch.object(brightness.subprocess, 'run') as run:
                service = brightness.BrightnessService(None)
                run.return_value = subprocess.CompletedProcess([], 1, '{"error":"DDC desligado"}', '')
                with self.assertRaisesRegex(ValueError, 'DDC desligado'):
                    service.read({'connector': 'HDMI-1'})
                run.side_effect = subprocess.TimeoutExpired([], 20)
                with self.assertRaisesRegex(ValueError, 'não respondeu'):
                    service.read({'connector': 'HDMI-1'})

    def test_ambiguous_connector_never_changes_an_arbitrary_display(self):
        with tempfile.TemporaryDirectory() as directory:
            for card in ('card1', 'card2'):
                path = Path(directory) / (card + '-HDMI-A-1')
                path.mkdir()
                (path / 'edid').write_bytes(bytes(128))
                (path / 'status').write_text('connected')
            with patch.object(brightness, 'DRM_DIR', Path(directory)), patch.object(brightness.subprocess, 'run') as run:
                with self.assertRaisesRegex(ValueError, 'identificar'):
                    brightness.BrightnessService(None).set({'connector': 'HDMI-1'}, 50)
                run.assert_not_called()


class DisplayBrightnessTests(unittest.TestCase):
    def setUp(self):
        self.layout = core.demo_layout()
        self.service = core.DisplayService.__new__(core.DisplayService)
        self.service.call = Mock()
        self.service.state = Mock(return_value=hardware_state(self.layout))
        self.service.brightness = Mock()
        self.service.brightness.read.return_value = 80

    def test_capture_reads_each_monitors_actual_brightness(self):
        self.service.brightness.read.side_effect = [37, 65]
        result = self.service.capture()
        self.assertEqual([m['brightness'] for m in result['monitors']], [37, 65])

    def test_unavailable_brightness_does_not_break_layout_capture(self):
        self.service.brightness.read.side_effect = [ValueError('Sem DDC/CI'), 65]
        result = self.service.capture()
        self.assertNotIn('brightness', result['monitors'][0])
        self.assertEqual(result['monitors'][0]['brightness_error'], 'Sem DDC/CI')
        self.assertEqual(result['monitors'][1]['brightness'], 65)

    def test_apply_changes_both_real_brightness_values(self):
        self.service.apply(self.layout)
        self.assertEqual([call.args[1] for call in self.service.brightness.set.call_args_list], [70, 50])

    def test_verify_never_writes_brightness(self):
        self.service.apply(self.layout, verify=True)
        self.service.brightness.set.assert_not_called()
        self.assertEqual(self.service.call.call_args.args[1].unpack()[1], 0)

    def test_old_profiles_do_not_modify_brightness(self):
        for m in self.layout['monitors']:
            del m['brightness']
        self.service.apply(self.layout)
        self.service.brightness.read.assert_not_called()
        self.service.brightness.set.assert_not_called()

    def test_disabled_monitor_is_not_written(self):
        self.layout['monitors'][1]['enabled'] = False
        self.service.apply(self.layout)
        self.assertEqual(self.service.brightness.set.call_count, 1)
        self.assertEqual(self.service.brightness.set.call_args.args[0]['connector'], 'eDP-1')

    def test_invalid_brightness_fails_before_layout_changes(self):
        self.layout['monitors'][0]['brightness'] = 101
        with self.assertRaises(ValueError):
            self.service.apply(self.layout)
        self.service.call.assert_not_called()
        self.service.brightness.set.assert_not_called()

    def test_partial_failure_restores_both_monitors(self):
        self.service.brightness.set.side_effect = [70, ValueError('Falhou'), 80, 80]
        with self.assertRaisesRegex(ValueError, 'Falhou'):
            self.service.apply(self.layout)
        self.assertEqual([call.args[1] for call in self.service.brightness.set.call_args_list], [70, 50, 80, 80])

    def test_current_brightness_does_not_apply_or_save_layout(self):
        monitor = self.layout['monitors'][1]
        self.service.brightness.set.return_value = 35
        self.assertEqual(self.service.set_brightness(monitor, 35), 35)
        self.service.call.assert_not_called()
        self.assertEqual(self.service.brightness.set.call_args.args[0]['connector'], 'HDMI-1')

    def test_replaced_monitor_is_rejected_even_if_same_model(self):
        monitor = dict(self.layout['monitors'][1], serial='different-serial')
        with self.assertRaisesRegex(ValueError, 'diferente'):
            self.service.set_brightness(monitor, 35)
        self.service.brightness.set.assert_not_called()

    def test_recovery_includes_original_brightness(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(core, 'CACHE_DIR', Path(directory)), patch.object(core.subprocess, 'Popen'):
            service = Mock()
            original = copy.deepcopy(self.layout)
            original['monitors'][0]['brightness'] = 91
            original['monitors'][1]['brightness'] = 43
            service.capture.return_value = original
            path = core.begin_preview(service, self.layout)
            core.revert_preview(service, path)
            service.apply.assert_called_with(original, trusted_restore=True)



if __name__ == '__main__':
    unittest.main(verbosity=2)
