import glob
import py_compile

files = glob.glob("*.py") + glob.glob("core/*.py") + glob.glob("routers/*.py")
for f in files:
    py_compile.compile(f, doraise=True)
print("COMPILE_OK", len(files), "files")

from core import automation

info = automation.scan_state_cookies()
print("scan:", info)
