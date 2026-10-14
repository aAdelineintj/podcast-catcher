#!/bin/sh
# PocketBay 构建钩子（由 package.json 的 "build" 调用）。
#
# 平台是单服务架构：公网入口只指向一个容器，前端与后端必须同容器共存。
# 平台生成的构建流程最后一步是 `COPY --from=build /app /app`，
# 所以只要把 Python 运行时装进 /app 内部，它就会跟着进运行镜像。
#
# 运行镜像是全新的 Alpine，只拷贝 /app —— apk 装到 /usr 的东西不会带过去。
set -eu

PYROOT=/app/.pysys
MIRROR_HOST=pypi.tuna.tsinghua.edu.cn

# 本地（macOS / 无 apk 环境）只构建前端，方便日常开发
if ! command -v apk >/dev/null 2>&1; then
  echo "[pb-build] 未检测到 apk（本地环境），跳过 Python 运行时安装"
  ./node_modules/.bin/next build
  exit 0
fi

echo "[pb-build] 环境：alpine $(cat /etc/alpine-release 2>/dev/null || echo '?')  arch $(uname -m)"

echo "[pb-build] 1/3 准备自带 Python 运行时 -> ${PYROOT}"
rm -rf "${PYROOT}"
mkdir -p "${PYROOT}/etc/apk/keys"
# 关键：把宿主的 apk 源与签名公钥一起带进新 rootfs，否则索引校验报 UNTRUSTED signature
cp -a /etc/apk/repositories "${PYROOT}/etc/apk/repositories" 2>/dev/null || true
cp -a /etc/apk/keys/. "${PYROOT}/etc/apk/keys/" 2>/dev/null || true

# 编译器与 Python 头文件都只装在宿主：不会进运行镜像，且路径与编译器期望的一致。
# webrtcvad-wheels 在 cp314 上没有预编译轮子，必须现场编译，需要 cc + Python.h。
apk add --no-cache build-base python3-dev
# PYROOT 只装运行时，保持体积最小
apk add --root "${PYROOT}" --initdb --no-cache python3 py3-pip

PYBIN="${PYROOT}/usr/bin/python3"
export PYTHONHOME="${PYROOT}/usr"
export LD_LIBRARY_PATH="${PYROOT}/usr/lib${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"

PYV="$("${PYBIN}" -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
echo "[pb-build]   python ${PYV}，prefix ${PYROOT}/usr"

# 编译器默认从 /usr/include/pythonX.Y 找 Python.h；宿主的 python3-dev 正是装在这里
export CFLAGS="-I/usr/include/python${PYV} ${CFLAGS:-}"
export CPPFLAGS="-I/usr/include/python${PYV} ${CPPFLAGS:-}"
if [ -f "/usr/include/python${PYV}/Python.h" ]; then
  echo "[pb-build]   头文件就绪：/usr/include/python${PYV}/Python.h"
else
  echo "[pb-build]   警告：未找到 /usr/include/python${PYV}/Python.h，现有目录："
  ls -d /usr/include/python* 2>/dev/null || echo "     （无任何 python 头文件目录）"
fi

echo "[pb-build] 2/3 安装后端依赖 -> ${PYROOT}/site"
PIP_ARGS=""
if wget -q -T 8 -O /dev/null "https://${MIRROR_HOST}/simple/" 2>/dev/null; then
  echo "[pb-build]   镜像源可达，使用 ${MIRROR_HOST}"
  PIP_ARGS="-i https://${MIRROR_HOST}/simple --trusted-host ${MIRROR_HOST}"
else
  echo "[pb-build]   镜像源不可达，使用官方 PyPI"
fi
# shellcheck disable=SC2086
"${PYBIN}" -m pip install --no-cache-dir --upgrade --target "${PYROOT}/site" \
  ${PIP_ARGS} -r backend/requirements.txt

echo "[pb-build] 依赖自检："
PYTHONPATH="${PYROOT}/site" "${PYBIN}" -c "
import importlib
failed = []
for name in ('fastapi','uvicorn','av','numpy','webrtcvad','tencentcloud','requests','dotenv'):
    try:
        m = importlib.import_module(name)
        print('  ok  ', name, getattr(m, '__version__', ''))
    except Exception as exc:
        print('  FAIL', name, exc)
        failed.append(name)
if failed:
    raise SystemExit('依赖缺失: ' + ', '.join(failed))
"

echo "[pb-build] 3/3 构建前端"
./node_modules/.bin/next build

echo "[pb-build] 完成"
