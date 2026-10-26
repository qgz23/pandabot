# -*- coding: utf-8 -*-
"""
服务器启动入口
================================================================
本地开发用 `nb run`，那是 nb-cli 的命令，它靠读 pyproject.toml 决定加载什么。
服务器上没必要装 nb-cli —— 直接执行这个文件就行：

    python bot.py

所以这里把加载顺序写死，不依赖 nb-cli，也不依赖 pyproject 里的插件列表：

    1. 定环境为 prod，读 .env 和 .env.prod
    2. 注册 QQ 适配器
    3. 加载 src/plugins 下所有插件
    4. 起服务

为什么 env 要提前设：
    NoneBot 在 init 那一刻就决定读哪个配置文件。ENVIRONMENT 没设的话
    默认是 prod，但显式写死更保险 —— 服务器上少一个变量没设就少一个坑。

服务器上不装 nonebot-plugin-docs / htmlkit 也能跑，只有这两个会用到它们：
    这个入口不加载它们，所以依赖可以少装两个。
"""

import os

# 必须在 import nonebot 之前设，init 时才读得到
os.environ.setdefault("ENVIRONMENT", "prod")

import nonebot
from nonebot.adapters.qq import Adapter

nonebot.init()

driver = nonebot.get_driver()
driver.register_adapter(Adapter)

# 插件目录跟 pyproject.toml 的 plugin_dirs 保持一致
nonebot.load_plugins("src/plugins")

if __name__ == "__main__":
    nonebot.run()
