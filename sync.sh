#!/bin/bash
# Mutsumi's SYNC 同步脚本
# 用法: ./sync.sh [--dry-run]

DRY_RUN=""

if [ "$1" = "--dry-run" ]; then
    DRY_RUN="--dry-run"
    echo "[Dry Run Mode] 只显示要同步的文件，不实际执行"
fi

HOST="root@code.arcol.site"
REMOTE_DIR="/home/ubuntu/gits/mutsumi-sync"

echo "======== Mutsumi's SYNC 同步 ========"
echo ""
echo "同步配置: config.yaml, requirements.txt, README.md"

if [ -z "$DRY_RUN" ]; then
    scp -o StrictHostKeyChecking=no config.yaml requirements.txt README.md $HOST:$REMOTE_DIR/
    echo "[√] 配置文件同步完成"
else
    echo "[dry-run] scp config.yaml requirements.txt README.md $HOST:$REMOTE_DIR/"
fi

echo ""
echo "同步源代码: src/"

if [ -z "$DRY_RUN" ]; then
    scp -o StrictHostKeyChecking=no -r src $HOST:$REMOTE_DIR/
    echo "[√] 源代码同步完成"
else
    echo "[dry-run] scp -r src $HOST:$REMOTE_DIR/"
fi

echo ""
echo "======== 同步完成 ========"
echo ""
echo "在服务器上运行测试:"
echo "  cd $REMOTE_DIR && python3 -m pytest tests/ -v"