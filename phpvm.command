#!/bin/zsh
# phpvm（macOS/Linux）：Finder 双击启动脚本（等价于在项目目录执行 python3 main.py）。
cd "$(dirname "$0")" || exit 1

# 优先使用带 Tk 的 Homebrew Python（python-tk@3.13），其次 PATH 中的 python3
PY=""
for cand in \
    /opt/homebrew/opt/python@3.13/bin/python3.13 \
    /opt/homebrew/bin/python3.12 \
    python3; do
    if command -v "$cand" >/dev/null 2>&1 || [ -x "$cand" ]; then
        if "$cand" -c "import tkinter" >/dev/null 2>&1; then
            PY="$cand"
            break
        fi
    fi
done

if [ -z "$PY" ]; then
    echo "phpvm 需要带 Tk 的 Python，请先执行："
    echo "  brew install python-tk@3.13"
    read -r "?按回车退出..."
    exit 1
fi

exec "$PY" main.py
