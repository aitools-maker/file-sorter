#!/bin/bash
# ダブルクリックで起動 → ブラウザでファイル仕分けアプリが開きます。
# 停止するときは、開いたターミナル窓で Control + C。
cd "$(dirname "$0")"
exec python3 server.py
