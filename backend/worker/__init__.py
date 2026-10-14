"""云端任务执行器（阶段二）。

模块划分：

- `config.py`           配置读取与启动校验
- `supabase_client.py`  PostgREST + Storage REST 薄封装
- `worker.py`           主循环与单集处理
- `selftest.py`         不开 Supabase 账号也能跑的本地自检（打桩服务器）

启动：`cd backend && ../venv/bin/python -m worker.worker`
自检：`cd backend && ../venv/bin/python -m worker.selftest`
"""
