# Windows onedir distribution. The same executable dispatches --worker.
# console=True preserves stdin/stderr for authenticated worker IPC.
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

a = Analysis(['app.py'], pathex=[], binaries=[], datas=collect_data_files('tzdata'),
             hiddenimports=collect_submodules('mt5_trader') + ['MetaTrader5'],
             hookspath=[], runtime_hooks=[], excludes=[], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='CFDInterestARB',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=True)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='CFDInterestARB')
