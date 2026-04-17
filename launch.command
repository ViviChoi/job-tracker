#!/bin/bash
# Job Tracker - 双击启动配置界面

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

# 找 Python 3.10+（python-jobspy 要求）
PYTHON=""
for cmd in python3.13 python3.12 python3.11 python3.10; do
  if command -v "$cmd" &>/dev/null; then
    PYTHON="$cmd"
    break
  fi
done

if [ -z "$PYTHON" ]; then
  echo "❌ 未找到 Python 3.10 或更高版本（python-jobspy 要求 3.10+）"
  echo ""
  echo "请安装 Python 3.12："
  echo "  brew install python@3.12"
  echo ""
  echo "安装完成后重新双击启动。"
  read -p "按回车键退出..."
  exit 1
fi

echo "使用 Python：$($PYTHON --version)"

# 若 venv 不存在或 Python 版本不匹配则重建
VENV_PYTHON=".venv/bin/python3"
REBUILD=0

if [ ! -d ".venv" ]; then
  REBUILD=1
else
  # 检查 venv 的 Python 版本是否满足 3.10+
  VENV_VER=$("$VENV_PYTHON" -c "import sys; print(sys.version_info[:2])" 2>/dev/null)
  if [[ "$VENV_VER" < "(3, 10)" ]]; then
    echo "检测到旧版 venv（Python < 3.10），重建中..."
    rm -rf .venv
    REBUILD=1
  fi
fi

if [ "$REBUILD" -eq 1 ]; then
  echo "首次启动，正在安装依赖..."
  "$PYTHON" -m venv .venv
fi

source .venv/bin/activate

pip install -q -r requirements.txt

# 二次核验关键包是否真的装上了
MISSING_PKGS=""
for pkg in jobspy flask requests gspread anthropic openai PyPDF2 docx; do
  if ! .venv/bin/python -c "import $pkg" &>/dev/null; then
    MISSING_PKGS="$MISSING_PKGS $pkg"
  fi
done

if [ -n "$MISSING_PKGS" ]; then
  echo ""
  echo "⚠️  以下包安装后仍无法导入，正在重试：$MISSING_PKGS"
  pip install -r requirements.txt
  STILL_MISSING=""
  for pkg in jobspy flask requests gspread anthropic openai PyPDF2 docx; do
    if ! .venv/bin/python -c "import $pkg" &>/dev/null; then
      STILL_MISSING="$STILL_MISSING $pkg"
    fi
  done
  if [ -n "$STILL_MISSING" ]; then
    echo ""
    echo "❌ 以下包安装失败：$STILL_MISSING"
    echo "   请检查网络后重试，或在终端手动运行："
    echo "   cd \"$DIR\" && source .venv/bin/activate && pip install -r requirements.txt"
    echo ""
    read -p "按回车键退出..."
    exit 1
  fi
fi

echo "安装完成"
echo "启动 Job Tracker 配置界面..."
.venv/bin/python setup.py
