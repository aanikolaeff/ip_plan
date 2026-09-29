#!/bin/bash
# Поднимает версию сборки на 0.1. Запускать перед деплоем: номер виден в углу
# страницы, и по нему сразу понятно, доехала ли выкатка.
#
#   ./bump_version.sh          # 0.1 → 0.2
#   ./bump_version.sh 1.0      # выставить конкретное значение
set -euo pipefail
cd "$(dirname "$0")"

old=$(cat VERSION 2>/dev/null || echo "0.0")
if [ $# -ge 1 ]; then
    new="$1"
else
    new=$(awk -v v="$old" 'BEGIN { printf "%.1f", v + 0.1 }')
fi
echo "$new" > VERSION
echo "версия: $old → $new"
echo "не забудьте закоммитить VERSION вместе с остальными изменениями"
