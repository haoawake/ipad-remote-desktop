#!/bin/bash
# 装 macOS 打包要用的依赖（.github/workflows/release.yml 和开发期 CI 共用，保证测的和发的是同一套）。
#     bash scripts/mac_deps.sh          # 用当前的 python
set -euo pipefail
PY="${PYTHON:-python}"
"$PY" -m pip install --disable-pip-version-check -r requirements-mac.txt pyinstaller

# numpy 在新系统上默认装的是“只支持 macOS 14 及以上”的那份（用系统的 Accelerate）。
# 换成同一版本里支持 macOS 11 起的那份（自带 OpenBLAS），还在用 macOS 12、13 的 Mac 也能打开。
# 本程序只用 numpy 比对画面哪里变了，两份没有区别。找不到就算了（最低系统版本由 build_mac.py 如实算出来）。
pyver=$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
npver=$("$PY" -c 'import numpy; print(numpy.__version__)')
wh=$(mktemp -d)
if "$PY" -m pip download --disable-pip-version-check --only-binary=:all: --no-deps --platform macosx_11_0_arm64 \
     --python-version "$pyver" -d "$wh" "numpy==$npver"; then
  "$PY" -m pip install --disable-pip-version-check --force-reinstall --no-deps "$wh"/numpy-*.whl
else
  echo "::warning::没找到支持 macOS 11 的 numpy $npver"
fi
"$PY" -c 'import numpy; print("numpy", numpy.__version__, numpy.__file__)'
