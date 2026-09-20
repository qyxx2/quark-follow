#!/bin/bash

cd /root/scripts/quark-follow || exit 1

# 使用虚拟环境中的 Python
PYTHON="/root/scripts/quark-follow/.venv/bin/python3"

# Web 登录账号
export QUARK_WEB_USERNAME='admin'
export QUARK_WEB_PASSWORD='替换成你自己的强密码'

# 固定的 Session 密钥，不要每次启动都改变
export QUARK_WEB_SECRET='替换成一串足够长的随机字符串'

# 启动 Web
exec "$PYTHON" web_app.py
