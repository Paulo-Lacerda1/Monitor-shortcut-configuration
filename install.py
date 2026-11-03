#!/usr/bin/python3
"""Install for the current user; no sudo or Python package downloads."""
import argparse
import os
from pathlib import Path
import shutil
import sys

SOURCE = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description='Instalar Monitores e Atalhos no utilizador atual')
    parser.add_argument('--uninstall', action='store_true')
    parser.add_argument('--destdir', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    home = (args.destdir or Path.home()).resolve()
    local = home / '.local'
    target = local / 'share/monitores-atalhos'
    desktop = local / 'share/applications/pt.local.MonitoresAtalhos.desktop'
    icon = local / 'share/icons/hicolor/scalable/apps/pt.local.MonitoresAtalhos.svg'
    launcher = local / 'bin/monitores-atalhos'
    if args.uninstall:
        if not args.destdir:
            sys.path.insert(0, str(target if target.exists() else SOURCE))
            import core
            bindings = core.Bindings()
            store = core.ProfileStore()
            for profile in store.profiles:
                # Restore only shortcuts whose command still belongs to this app.
                path = profile.get('binding_path')
                if path and path in bindings.paths() and str(target / 'app.py') in bindings.read(path)['command']:
                    bindings.remove(profile)
        for item in (desktop, icon, launcher):
            item.unlink(missing_ok=True)
        if target.exists():
            shutil.rmtree(target)
        print('Aplicação removida. Os perfis foram preservados em ~/.config/monitores-atalhos/.')
        return 0
    if not args.destdir:
        try:
            import gi
            gi.require_version('Gtk', '4.0')
            gi.require_version('Adw', '1')
            from gi.repository import Gtk, Adw
            import cairo
        except (ImportError, ValueError):
            print('Instala primeiro as dependências do Ubuntu:\nsudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1', file=sys.stderr)
            return 1
    target.mkdir(parents=True, exist_ok=True)
    for name in ('app.py', 'core.py', 'brightness.py', 'README.md', 'icon.svg', 'install.py', 'tests.py', 'LICENSE'):
        if (SOURCE / name).resolve() != (target / name).resolve():
            shutil.copy2(SOURCE / name, target / name)
    for directory in (desktop.parent, icon.parent, launcher.parent):
        directory.mkdir(parents=True, exist_ok=True)
    shutil.copy2(target / 'icon.svg', icon)
    # Desktop Exec has its own quoting rules, distinct from shell quoting.
    def desktop_quote(value):
        return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$').replace('%', '%%') + '"'
    desktop.write_text('[Desktop Entry]\nType=Application\nVersion=1.0\nName=Monitores e Atalhos\n'
        'Comment=Organizar monitores e guardar disposições com atalhos\n'
        f'Exec=/usr/bin/python3 {desktop_quote(target / "app.py")}\n'
        'Icon=pt.local.MonitoresAtalhos\nTerminal=false\nCategories=Settings;HardwareSettings;\n'
        'StartupNotify=true\nStartupWMClass=pt.local.MonitoresAtalhos\n')
    import shlex
    launcher.write_text('#!/bin/sh\nexec /usr/bin/python3 ' + shlex.quote(str(target / 'app.py')) + ' "$@"\n')
    launcher.chmod(0o755)
    print('Instalado. Procura «Monitores e Atalhos» no menu de aplicações do Ubuntu.')
    print('Também podes abrir com: ' + str(launcher))
    return 0


if __name__ == '__main__':
    sys.exit(main())
