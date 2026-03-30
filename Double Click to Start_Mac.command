#!/bin/bash
# Job Tracker — 双击启动

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║           Job Tracker 正在启动...            ║"
echo "╚══════════════════════════════════════════════╝"
echo ""

# ── 加载 Homebrew 路径（双击启动时 Finder 不加载 shell PATH）──
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:$PATH"

# ── Python 环境检测 ──────────────────────────────────────────
#
#  最低要求：Python 3.10+
#  原因：依赖 python-jobspy（LinkedIn 职位抓取），该库官方不支持 3.9 及以下版本
#

PYTHON_CMD=""
MIN_VER=310

echo "[ 1/4 ] 检测 Python 环境..."

for cmd in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$cmd" &>/dev/null; then
    VER=$("$cmd" -c "import sys; v=sys.version_info; print(v.major*100+v.minor)" 2>/dev/null)
    if [ -n "$VER" ] && [ "$VER" -ge $MIN_VER ]; then
      PYTHON_CMD="$cmd"
      break
    fi
  fi
done

if [ -z "$PYTHON_CMD" ]; then
  INSTALLED_VER=$(python3 --version 2>&1 || echo "未检测到")
  echo ""
  echo "  ❌ Python 版本不满足要求"
  echo ""
  echo "  ┌─ 缺少什么 ──────────────────────────────────────"
  echo "  │  需要：Python 3.10 或更高版本"
  echo "  │  当前：$INSTALLED_VER"
  echo "  │  原因：python-jobspy（LinkedIn 抓取库）要求 Python ≥ 3.10"
  echo "  └────────────────────────────────────────────────"

  if command -v brew &>/dev/null; then
    echo ""
    echo "  检测到 Homebrew，尝试自动安装 Python 3.12..."
    brew install python@3.12
    for cmd in python3.13 python3.12 python3.11 python3.10; do
      if command -v "$cmd" &>/dev/null; then
        VER=$("$cmd" -c "import sys; v=sys.version_info; print(v.major*100+v.minor)" 2>/dev/null)
        if [ -n "$VER" ] && [ "$VER" -ge $MIN_VER ]; then
          PYTHON_CMD="$cmd"
          break
        fi
      fi
    done
  fi

  if [ -z "$PYTHON_CMD" ]; then
    echo ""
    echo "  ┌─ 手动安装方式 ───────────────────────────────────"
    echo "  │"
    echo "  │  方式一（推荐，使用 Homebrew）："
    echo "  │    brew install python@3.12"
    echo "  │"
    echo "  │  方式二（官网安装包）："
    echo "  │    https://www.python.org/downloads/"
    echo "  │    下载 3.10 / 3.11 / 3.12 均可"
    echo "  │"
    echo "  │  安装后重新双击此脚本即可"
    echo "  └────────────────────────────────────────────────"
    echo ""
    read -p "  按任意键退出..."
    exit 1
  fi
fi

echo "       ✅ $($PYTHON_CMD --version)"

# ── 虚拟环境 ─────────────────────────────────────────────────

echo "[ 2/4 ] 准备虚拟环境..."

if [ -d ".venv" ]; then
  VENV_VER=$(".venv/bin/python" -c "import sys; v=sys.version_info; print(v.major*100+v.minor)" 2>/dev/null)
  if [ -z "$VENV_VER" ] || [ "$VENV_VER" -lt $MIN_VER ]; then
    VENV_OLD=$(".venv/bin/python" --version 2>/dev/null || echo "版本未知")
    echo "       ⚠️  旧环境版本（$VENV_OLD）低于 3.10，python-jobspy 无法运行"
    echo "       正在用 $($PYTHON_CMD --version) 重新创建..."
    rm -rf .venv
  fi
fi

if [ ! -d ".venv" ]; then
  "$PYTHON_CMD" -m venv .venv
  if [ $? -ne 0 ]; then
    echo ""
    echo "  ❌ 虚拟环境创建失败"
    echo "     可能原因：Python 安装不完整，或磁盘空间不足"
    echo "     请尝试重新安装 Python 后再启动"
    read -p "  按任意键退出..."
    exit 1
  fi
fi

source .venv/bin/activate
echo "       ✅ 虚拟环境就绪（$(.venv/bin/python --version)）"

# ── 依赖安装 ─────────────────────────────────────────────────

echo "[ 3/4 ] 检查并安装依赖..."

PIP_LOG=$(pip install -q -r requirements.txt 2>&1)
PIP_EXIT=$?

if [ $PIP_EXIT -ne 0 ]; then
  FAILED_PKG=$(echo "$PIP_LOG" | grep -E "^ERROR: (Could not find|No matching distribution)" | head -3)
  echo ""
  echo "  ❌ 依赖安装失败"
  echo ""
  if [ -n "$FAILED_PKG" ]; then
    echo "  ┌─ 失败原因 ──────────────────────────────────────"
    echo "$FAILED_PKG" | while read -r line; do
      echo "  │  $line"
    done
    echo "  └────────────────────────────────────────────────"
  fi
  echo ""
  echo "  常见原因及解决方法："
  echo "    • 网络问题  →  检查网络连接或挂载代理后重试"
  echo "    • pip 缓存  →  在终端运行："
  echo "      cd \"$DIR\" && source .venv/bin/activate && pip install -r requirements.txt"
  echo ""
  read -p "  按任意键退出..."
  exit 1
fi

echo "       ✅ 所有依赖就绪"

# ── 启动服务 ─────────────────────────────────────────────────

echo "[ 4/4 ] 启动服务..."

# 如果服务已在运行，直接开浏览器
if lsof -i :8080 &>/dev/null; then
  echo "       ⚡ 服务已在运行，直接打开浏览器"
  open "http://localhost:8080"
  exit 0
fi

.venv/bin/python setup.py &
SERVER_PID=$!

echo -n "       等待服务就绪"
for i in $(seq 1 30); do
  if curl -s "http://localhost:8080" &>/dev/null; then
    echo " ✅"
    echo ""
    echo "  🎉 Job Tracker 已就绪！浏览器已自动打开"
    echo "     地址：http://localhost:8080"
    echo "     按 Ctrl+C 停止服务"
    echo ""
    open "http://localhost:8080"
    break
  fi
  if ! kill -0 $SERVER_PID 2>/dev/null; then
    echo ""
    echo ""
    echo "  ❌ 服务启动失败"
    echo "     请向上滚动查看具体错误信息"
    echo "     如果看到端口冲突，请关闭其他占用 8080 端口的程序"
    read -p "  按任意键退出..."
    exit 1
  fi
  echo -n "."
  sleep 1
done

# 保持终端开着，显示服务日志（Ctrl+C 可停止）
wait $SERVER_PID
