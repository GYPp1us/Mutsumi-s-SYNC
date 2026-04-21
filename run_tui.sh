#!/bin/bash
# 在 tmux 中运行 TUI

SESSION="mutsumi-tui"

# 如果 session 已存在则退出
tmux kill-session -t $SESSION 2>/dev/null

# 创建新 session 并运行 TUI
tmux new-session -d -s $SESSION "cd /home/ubuntu/gits/mutsumi-sync && /usr/bin/python start_tui.py"

echo "TUI 已在 tmux session '$SESSION' 中启动"
echo "使用 'tmux attach -t $SESSION' 查看"
