import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET


@unittest.skipUnless(os.name == 'nt' and os.environ.get('RUN_WINDOWS_SERVICE_TEST') == '1',
                     'Opt-in process test: RUN_WINDOWS_SERVICE_TEST=1 (requires endpoint protection approval)')
class ServiceTests(unittest.TestCase):
    def test_child_restarts_and_stop_does_not_restart(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            exe = folder / 'service.exe'
            shutil.copy2(root / 'ServicoWindows' / 'HikvisionManagerService.exe', exe)
            worker = folder / 'worker.py'
            worker.write_text(
                "from pathlib import Path\nimport time\np=Path('attempts')\n"
                "n=int(p.read_text())+1 if p.exists() else 1\np.write_text(str(n))\n"
                "if n==1: raise SystemExit(1)\ntime.sleep(120)\n", encoding='utf-8')
            config = ET.Element('configuration')
            settings = ET.SubElement(config,'appSettings')
            for key,value in {'RootPath':folder,'PythonPath':sys.executable,'AppPath':worker,
                              'LogPath':folder/'service.log'}.items():
                ET.SubElement(settings,'add',key=key,value=str(value))
            ET.ElementTree(config).write(str(exe)+'.config',encoding='utf-8',xml_declaration=True)
            process = subprocess.Popen([str(exe),'--console'], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                deadline=time.monotonic()+15
                count=folder/'attempts'
                while time.monotonic()<deadline:
                    if count.exists() and count.read_text()=='2': break
                    time.sleep(0.1)
                self.assertTrue(count.exists())
                self.assertEqual(count.read_text(),'2')
            finally:
                try:
                    out,err=process.communicate(b'\n',timeout=20)
                except subprocess.TimeoutExpired:
                    subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True)
                    process.communicate(timeout=10)
                    raise
            self.assertEqual(process.returncode,0,err.decode(errors='replace'))
            self.assertIn('Reiniciando em 5', (folder/'service.log').read_text())
            self.assertEqual(count.read_text(),'2')


if __name__ == '__main__':
    unittest.main()
