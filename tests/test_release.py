import tempfile
import unittest
import zipfile
import os
import subprocess
from unittest.mock import patch
from pathlib import Path

from release import build_release, backup_private, restore_private, install_release, RELEASE_FILES
from storage import write_json, read_json


class ReleaseTest(unittest.TestCase):
    def test_manifest_write_error_reverts_upgrade_and_preserves_old_manifest(self):
        from release import write_bytes
        build_release(self.root,self.bundle)
        destination=self.root/'manifest-error'
        install_release(self.bundle,destination)
        before={str(p.relative_to(destination)):p.read_bytes() for p in destination.rglob('*') if p.is_file()}
        (self.root/'version.py').write_text("PRODUCT_VERSION='upgrade'\n")
        build_release(self.root,self.bundle)
        def fail_manifest(path,data):
            if path.name=='release-manifest.json':raise OSError('manifest disk error')
            write_bytes(path,data)
        with patch('release.write_bytes',side_effect=fail_manifest):
            with self.assertRaisesRegex(OSError,'manifest disk error'):install_release(self.bundle,destination)
        self.assertEqual({str(p.relative_to(destination)):p.read_bytes() for p in destination.rglob('*') if p.is_file()},before)

    def test_rollback_failure_is_explicit_instead_of_reporting_success(self):
        from release import write_bytes
        build_release(self.root,self.bundle)
        destination=self.root/'rollback-error';destination.mkdir()
        (destination/'akshare_worker.py').write_bytes(b'old')
        calls=0
        def fail_write_and_rollback(path,data):
            nonlocal calls
            calls+=1
            if calls>=3:raise OSError('persistent disk error')
            write_bytes(path,data)
        with patch('release.write_bytes',side_effect=fail_write_and_rollback):
            with self.assertRaisesRegex(OSError,'自动回退未完成'):install_release(self.bundle,destination)

    def test_partial_install_and_restore_errors_revert_all_changed_files(self):
        from release import write_bytes
        build_release(self.root,self.bundle)
        backup=self.root/'backup.zip';backup_private(self.root,backup)
        for operation,bundle in ((install_release,self.bundle),(restore_private,backup)):
            with self.subTest(operation=operation.__name__):
                destination=self.root/(operation.__name__+'-partial');destination.mkdir()
                files=['akshare_worker.py','dashboard-visuals.js','release-manifest.json'] if operation is install_release else ['.env','data/publications/2026-09-30.json']
                for name in files:
                    path=destination/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'old')
                before={str(p.relative_to(destination)):p.read_bytes() for p in destination.rglob('*') if p.is_file()}
                calls=0
                def fail_once(path,data):
                    nonlocal calls
                    calls+=1
                    if calls==3:raise OSError('fixture disk error')
                    write_bytes(path,data)
                with patch('release.write_bytes',side_effect=fail_once):
                    with self.assertRaisesRegex(OSError,'fixture disk error'):operation(bundle,destination)
                after={str(p.relative_to(destination)):p.read_bytes() for p in destination.rglob('*') if p.is_file()}
                self.assertEqual(after,before)

    def test_actual_source_dependencies_are_checked_before_build(self):
        from release import ROOT,runtime_dependencies
        self.assertEqual(runtime_dependencies(ROOT,RELEASE_FILES),set())
        self.assertIn('research_facts.py',runtime_dependencies(ROOT,[n for n in RELEASE_FILES if n!='research_facts.py']))
        source=self.root/'dashboard.py'
        source.write_text('def read_report():\n    import missing_dependency\n',encoding='utf-8')
        (self.root/'missing_dependency.py').write_text('value=1',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'missing_dependency.py'):build_release(self.root,self.bundle)

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        for name in RELEASE_FILES:
            path=self.root/name
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text('test',encoding='utf-8')
        (self.root/'version.py').write_text("PRODUCT_VERSION='test'\n",encoding='utf-8')
        write_json(self.root/'config.json',{'watchlist':['private']})
        (self.root/'.env').write_text('PRIVATE_KEY',encoding='utf-8')
        write_json(self.root/'data/publications/2026-09-30.json',{'private':'report'})
        self.bundle=self.root/'release.zip'

    def test_release_manifest_excludes_private_files_and_rejects_tampering(self):
        build_release(self.root,self.bundle)
        with zipfile.ZipFile(self.bundle) as archive:
            self.assertNotIn('.env',archive.namelist())
            self.assertNotIn('config.json',archive.namelist())
            self.assertFalse(any(n.startswith('data/') for n in archive.namelist()))
        destination=self.root/'clean'
        install_release(self.bundle,destination)
        self.assertEqual((destination/'version.py').read_bytes(),(self.root/'version.py').read_bytes())
        with zipfile.ZipFile(self.bundle,'a') as archive: archive.writestr('dashboard.py','changed')
        with self.assertRaises(ValueError): install_release(self.bundle,self.root/'bad')
        self.assertFalse((self.root/'bad').exists())

    def test_restore_is_hash_verified_and_keeps_keys_local(self):
        backup=self.root/'private-backup.zip'
        backup_private(self.root,backup)
        write_json(self.root/'config.json',{'watchlist':[]})
        (self.root/'.env').write_text('changed',encoding='utf-8')
        restore_private(backup,self.root)
        self.assertEqual(read_json(self.root/'config.json')['watchlist'],['private'])
        self.assertEqual((self.root/'.env').read_text(),'PRIVATE_KEY')
        self.assertEqual(read_json(self.root/'data/publications/2026-09-30.json')['private'],'report')

    def test_rollback_does_not_replace_private_data(self):
        build_release(self.root,self.bundle)
        (self.root/'version.py').write_text("PRODUCT_VERSION='upgrade'\n",encoding='utf-8')
        install_release(self.bundle,self.root)
        self.assertIn("'test'",(self.root/'version.py').read_text())
        self.assertEqual(read_json(self.root/'config.json')['watchlist'],['private'])
        self.assertEqual((self.root/'.env').read_text(),'PRIVATE_KEY')

    @unittest.skipUnless(os.name=='nt','Windows directory junction check')
    def test_install_and_restore_reject_junction_escape_before_any_write(self):
        source=self.root/'tests/probe.py';source.parent.mkdir();source.write_text('probe')
        build_release(self.root,self.bundle)
        backup=self.root/'backup.zip';backup_private(self.root,backup)
        for operation,bundle,folder in ((install_release,self.bundle,'tests'),(restore_private,backup,'data')):
            with self.subTest(operation=operation.__name__):
                destination=self.root/operation.__name__;destination.mkdir()
                outside=self.root/(operation.__name__+'-outside');outside.mkdir()
                link=destination/folder
                quote=lambda p:"'"+str(p).replace("'","''")+"'"
                result=subprocess.run(['powershell.exe','-NoProfile','-Command',
                    'New-Item -ItemType Junction -Path '+quote(link)+' -Target '+quote(outside)+' | Out-Null'],
                    capture_output=True)
                if result.returncode:self.skipTest('Directory junction creation unavailable')
                with self.assertRaisesRegex(ValueError,'目标路径越界'):operation(bundle,destination)
                self.assertFalse((destination/'version.py').exists())
                self.assertFalse((destination/'.env').exists())
                self.assertEqual(list(outside.iterdir()),[])
