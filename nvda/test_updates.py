"""Published update selection, streamed integrity and failed-transfer recovery."""
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import ast
import types
import sys
import weakref
import zipfile
from unittest.mock import patch
from dataclasses import replace
from _reloq_updates import check, download, resolve, select, Update, version


def _download_url(name):
    return 'https://git.serrebiradio.com/serrebi/Reloq-C/releases/download/v1.2.3/' + name


class Updates(unittest.TestCase):
    def setUp(self):
        self.bytes = b'fixture package\0' * 5000
        self.name = 'reloq-c-synth-x64-1.2.3.nvda-addon'
        self.hex = hashlib.sha256(self.bytes).hexdigest()
        self.asset = dict(name=self.name, size=len(self.bytes),
            browser_download_url=_download_url(self.name))
        self.checksum = (self.hex + '  ' + self.name + '\n').encode('ascii')
        self.sidecar = dict(name=self.name + '.sha256', size=len(self.checksum),
            browser_download_url=_download_url(self.name + '.sha256'))
        self.feed = [{'assets': [self.asset, self.sidecar], 'draft': False}]

    def route(self, feed, checksum):
        def fetch(request, timeout=20):
            if request.full_url.endswith('.sha256'):
                return io.BytesIO(checksum)
            return io.BytesIO(json.dumps(feed).encode())
        return fetch

    def resolved(self, feed=None, checksum=None):
        candidate = select(self.feed if feed is None else feed, '1.2.2')
        return resolve(candidate, fetch=lambda *a, **kw: io.BytesIO(
            self.checksum if checksum is None else checksum))

    def test_published_versions_and_platform(self):
        data = [{'draft': True, 'assets': [self.asset, self.sidecar]},
                {'assets': [dict(name='other.apk', size=1, browser_download_url=_download_url('other.apk'))]}]
        self.assertIsNone(select(data, '1.2.2'))
        self.assertIsNone(select([], '1.2.2'))
        self.assertIsNone(select(self.feed, '1.2.3'))
        self.assertIsNone(select(self.feed, '1.2.4'))
        candidate = select(self.feed, '1.2.2')
        self.assertEqual(candidate.version, '1.2.3')
        self.assertEqual(candidate.sha256, '')
        self.assertEqual(candidate.checksum_url, self.sidecar['browser_download_url'])
        self.assertGreater(version('0.2.1000'), version('0.2.999'))
        update = check('1.2.2', fetch=self.route(self.feed, self.checksum))
        self.assertEqual(update, replace(candidate, sha256=self.hex))
        self.assertIsNone(check('1.2.3', fetch=self.route(self.feed, self.checksum)))

    def test_development_build_waits_for_matching_stable_release(self):
        def feed(number):
            name = f'reloq-c-synth-x64-{number}.nvda-addon'
            package = dict(name=name, size=len(self.bytes), browser_download_url=_download_url(name))
            sidecar = dict(name=name + '.sha256', size=len(self.checksum),
                browser_download_url=_download_url(name + '.sha256'))
            return [{'assets': [package, sidecar]}]
        self.assertIsNone(select(feed('1.2.2'), '1.2.3-dev1'))
        self.assertIsNone(select(feed('1.2.3-dev2'), '1.2.3-dev1'))
        self.assertEqual(select(feed('1.2.3'), '1.2.3-dev1').version, '1.2.3')
        self.assertIsNone(select(feed('1.2.3'), '1.2.3'))
        self.assertIsNone(select(feed('1.2.3'), '1.2.4-rc1'))
        self.assertEqual(select(feed('1.2.4'), '1.2.4-rc1').version, '1.2.4')

    def test_metadata_rejects_wrong_source_or_missing_integrity(self):
        alternate = dict(name='other.nvda-addon', size=1,
            browser_download_url=_download_url('other.nvda-addon'))
        cases = [
            [dict(self.asset, size=0), self.sidecar],
            [dict(self.asset, size=130*1024*1024), self.sidecar],
            [dict(self.asset, size=True), self.sidecar],
            [dict(self.asset, browser_download_url=self.asset['browser_download_url'].replace(
                'git.serrebiradio.com', 'example.net')), self.sidecar],
            [dict(self.asset, browser_download_url=self.asset['browser_download_url'].replace(
                '/serrebi/', '/someone/')), self.sidecar],
            [dict(self.asset, browser_download_url=None), self.sidecar],
            [self.asset],
            [self.asset, alternate],
            [self.asset, dict(self.sidecar, browser_download_url=None)],
            [self.asset, dict(self.sidecar, browser_download_url=self.sidecar[
                'browser_download_url'].replace('git.serrebiradio.com', 'example.net'))],
            [self.asset, dict(self.sidecar, browser_download_url=self.sidecar[
                'browser_download_url'].replace('/serrebi/', '/someone/'))],
            [self.asset, dict(self.sidecar, size=0)],
            [self.asset, dict(self.sidecar, size=4097)],
            [self.asset, dict(self.sidecar, size=True)],
        ]
        for assets in cases:
            with self.subTest(assets=assets), self.assertRaises(ValueError):
                select([{'assets': assets}], '1.2.2')

    def test_checksum_file_must_match_package(self):
        candidate = select(self.feed, '1.2.2')
        fetch = lambda data: lambda *a, **kw: io.BytesIO(data)
        self.assertEqual(resolve(candidate, fetch=fetch(self.checksum)).sha256, self.hex)
        bare = resolve(candidate, fetch=fetch((self.hex + '\n').encode('ascii')))
        self.assertEqual(bare.sha256, self.hex)
        binary = resolve(candidate, fetch=fetch((self.hex + ' *' + self.name + '\n').encode('ascii')))
        self.assertEqual(binary.sha256, self.hex)
        upper = resolve(candidate, fetch=fetch((self.hex.upper() + '  ' + self.name + '\n').encode('ascii')))
        self.assertEqual(upper.sha256, self.hex)
        for data in (b'', b'not a checksum\n', ('0' * 63 + '\n').encode('ascii'),
                     ('0' * 65 + '\n').encode('ascii'), ('z' * 64 + '\n').encode('ascii'),
                     (self.hex + '  other.nvda-addon\n').encode('ascii'),
                     (self.hex + '  ' + self.name + ' extra\n').encode('ascii'),
                     (self.hex + '  ' + self.name + '\n').encode('utf-16'),
                     b'x' * 4097):
            with self.subTest(data=data[:40]), self.assertRaises(ValueError):
                resolve(candidate, fetch=fetch(data))
        with self.assertRaises(ValueError):
            resolve(replace(candidate, checksum_url=''))

    def test_older_version_numbers_with_stable_filename(self):
        name = 'reloq-c-synth-x64-1.2.3.nvda-addon'
        package = dict(name=name, size=len(self.bytes), browser_download_url=_download_url(name))
        sidecar = dict(name=name + '.sha256', size=len(self.checksum),
            browser_download_url=_download_url(name + '.sha256'))
        for installed in ('1.2.0', '1.2.1', '1.2.2'):
            self.assertEqual(select([{'assets': [package, sidecar]}], installed).version, '1.2.3')
        self.assertIsNone(select([{'assets': [package, sidecar]}], '1.2.3'))

    def test_version_ordering_for_stable_filename(self):
        first = 'reloq-c-synth-x64-1.2.3.nvda-addon'
        second = 'reloq-c-synth-x64-1.2.4.nvda-addon'
        assets = []
        for name in (first, second):
            assets.append(dict(name=name, size=len(self.bytes), browser_download_url=_download_url(name)))
            assets.append(dict(name=name + '.sha256', size=len(self.checksum),
                browser_download_url=_download_url(name + '.sha256')))
        self.assertEqual(select([{'assets': assets}], '1.2.3').version, '1.2.4')
        self.assertIsNone(select([{'assets': assets}], '1.2.4'))

    def test_streamed_download_then_reject_corrupt_truncated_and_oversized(self):
        update = self.resolved()
        with tempfile.TemporaryDirectory() as folder:
            target = download(update, folder, fetch=lambda *a, **kw: io.BytesIO(self.bytes))
            self.assertEqual(target.read_bytes(), self.bytes)
            for data in (self.bytes[:-1], self.bytes+b'x', self.bytes.replace(b'fixture', b'corrupt')):
                with self.subTest(length=len(data)), self.assertRaises(ValueError):
                    download(update, folder, fetch=lambda *a, **kw: io.BytesIO(data))
                self.assertEqual(target.read_bytes(), self.bytes)
                self.assertFalse(Path(str(target)+'.part').exists())
            with self.assertRaises(ValueError):
                download(replace(update, name='../escape.nvda-addon'), folder)
            with self.assertRaises(ValueError):
                download(replace(update, sha256=''), folder)
            with self.assertRaises(ValueError):
                download(replace(update, sha256='0'*64), folder,
                         fetch=lambda *a, **kw: io.BytesIO(self.bytes))
            with self.assertRaises(ValueError):
                download(replace(update, checksum_url='https://example.net/x.sha256'), folder)

    def test_settings_check_download_and_native_installer_handoff(self):
        source = Path(__file__).resolve().parents[1] / 'addon/synth/globalPlugins/reloqCDictionaries/__init__.py'
        tree = ast.parse(source.read_text())
        panel_code = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'EloquenceSettingsPanel')
        class Control:
            def Enable(self, value): self.enabled = value
            def SetLabel(self, value): self.label = value
        class Thread:
            def __init__(self, *, target, **kw): self.target = target
            def start(self): self.target()
        for wrong_identity, closed, corrupt in ((False, False, False), (True, False, False),
                                                (False, True, False), (False, False, True)):
            with self.subTest(wrong_identity=wrong_identity, closed=closed, corrupt=corrupt):
                output = io.BytesIO()
                with zipfile.ZipFile(output, 'w') as archive:
                    archive.writestr('manifest.ini', json.dumps(dict(name='other' if wrong_identity else 'reloqC', version='1.2.3')))
                data = output.getvalue()
                package = dict(name=self.name, size=len(data), browser_download_url=_download_url(self.name))
                checksum = (hashlib.sha256(data).hexdigest() + '  ' + self.name + '\n').encode('ascii')
                sidecar = dict(name=self.name + '.sha256', size=len(checksum),
                    browser_download_url=_download_url(self.name + '.sha256'))
                candidate = resolve(select([{'assets': [package, sidecar]}], '1.2.2'),
                    fetch=lambda *a, **kw: io.BytesIO(checksum))
                paths, installed = [], []
                def transfer(update, folder):
                    path = download(update, folder, fetch=lambda *a, **kw: io.BytesIO(data + b'x' if corrupt else data))
                    paths.append(path)
                    return path
                def bundle(path):
                    with zipfile.ZipFile(path) as archive:
                        return types.SimpleNamespace(manifest=json.loads(archive.read('manifest.ini')))
                adapter = types.ModuleType('synthDrivers.reloqC')
                adapter._reloq_updates = types.SimpleNamespace(check=lambda v: candidate, download=transfer)
                install_module = types.ModuleType('gui.addonGui')
                def native_install(parent, path):
                    self.assertTrue(Path(path).is_file())
                    installed.append(path)
                    return True
                install_module.installAddon = native_install
                modules = {'synthDrivers': types.ModuleType('synthDrivers'), 'synthDrivers.reloqC': adapter,
                           'gui': types.ModuleType('gui'), 'gui.addonGui': install_module}
                environment = dict(SettingsPanel=object, _=lambda s: s, weakref=weakref, tempfile=tempfile,
                    threading=types.SimpleNamespace(Thread=Thread), addonHandler=types.SimpleNamespace(AddonBundle=bundle),
                    wx=types.SimpleNamespace(CallAfter=lambda fn, *args: fn(*args)), ui=types.SimpleNamespace(message=lambda s: None))
                exec(compile(ast.Module(body=[panel_code], type_ignores=[]), str(source), 'exec'), environment)
                panel = environment['EloquenceSettingsPanel']()
                panel.package_update = None; panel.package_pending = False; panel.closing = False
                panel.installed_name = 'reloqC'; panel.installed_version = '1.2.2'
                panel.package_button = Control(); panel.package_status = Control()
                with patch.dict(sys.modules, modules):
                    panel.on_package_update(None)
                    self.assertEqual(panel.package_update, candidate)
                    panel.closing = closed
                    panel.on_package_update(None)
                self.assertEqual(len(installed), 0 if wrong_identity or closed or corrupt else 1)
                self.assertTrue(all(not path.exists() for path in paths))


if __name__ == '__main__':
    unittest.main()
