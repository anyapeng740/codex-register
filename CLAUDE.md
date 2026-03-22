# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 常用命令

### 安装依赖
```bash
uv sync
# 或
pip install -r requirements.txt
```

### 启动开发环境
```bash
python webui.py
python webui.py --debug
python webui.py --host 0.0.0.0 --port 8080
python webui.py --access-password mypassword
```

### 打包
```bash
bash build.sh
# Windows
build.bat
```

`build.sh` / `build.bat` 最终都会执行：
```bash
pyinstaller codex_register.spec --clean --noconfirm
```

### Docker
```bash
docker-compose up -d
docker-compose logs -f
docker-compose down
docker-compose build --no-cache
```

### 数据库脚本
```bash
python -m src.database.init_db --check
python -m src.database.init_db --reset
```

### 测试与校验
仓库当前没有明确配置好的测试、lint、format 或 typecheck 命令。

- `pyproject.toml` 声明了 `pytest` 和 `httpx` 的可选开发依赖，但仓库中未发现测试目录、pytest 配置或 CI 测试步骤。
- 因此不要假设存在稳定可用的单测入口；如果需要补测，先检查当前改动附近是否已有测试约定。

## 高层架构

这是一个 **FastAPI + Jinja2 + 原生 JavaScript** 的单体 Web UI，围绕“账号注册任务”展开，核心是把邮箱服务、OpenAI OAuth/注册流程、代理策略、账号存储、导出/上传能力整合到一个本地可运行的后台里。

### 启动链路
- `webui.py`：CLI 入口。负责加载 `.env`、创建 `data/` 和 `logs/`、初始化数据库、配置日志，然后启动 Uvicorn。
- `src/web/app.py`：创建 FastAPI 应用，挂载 `/api` 路由、WebSocket 路由、模板和静态资源，并提供基于 cookie 的简单登录保护。
- `src/database/init_db.py` + `src/database/session.py`：初始化数据库连接、建表、SQLite 自动补列迁移、注入默认设置。

### 配置模型
- 运行时配置的**真实来源是数据库 `settings` 表**，不是静态配置文件。
- `src/config/settings.py` 维护所有配置项定义、默认值、分类和 secret 字段，并通过 `get_settings()` / `update_settings()` 暴露给全站使用。
- 环境变量主要用于启动前阶段，尤其是数据库路径与打包后的 `data/`、`logs/` 目录定位；应用启动后多数业务设置从数据库读取。
- 修改设置时，优先沿用现有的 `update_settings()` 路径，不要在各处直接读写数据库键值。

### 数据与状态分层
- **持久化状态**在数据库：账号、邮箱服务、注册任务记录、代理、上传服务配置、系统设置都在 SQLAlchemy 模型里，定义见 `src/database/models.py`。
- **实时状态**在内存：WebSocket 连接、任务日志队列、批量任务进度、取消标记由 `src/web/task_manager.py` 持有。
- 这意味着：页面刷新或 API 查询看的是数据库快照；实时日志和进度推送依赖当前进程内存，不是 Redis/队列系统。

### 注册主流程
- `src/web/routes/registration.py` 是任务编排入口：创建单次/批量注册任务、决定使用哪个邮箱服务、决定代理来源、提交线程池执行，并把状态同步到数据库和 `TaskManager`。
- `src/web/task_manager.py` 用全局 `ThreadPoolExecutor(max_workers=50)` 执行注册任务，并把日志广播到 WebSocket。
- `src/core/register.py` 是真正的注册引擎：
  - 创建邮箱
  - 启动 OAuth
  - 走 OpenAI 注册/登录 HTTP 流程
  - 拉取邮箱验证码
  - 保存账号结果与日志
- 代理选择策略不在 HTTP 客户端里硬编码，而是在 `registration.py` 先决策：**代理列表随机可用项 > 动态代理 API > 静态默认代理**。

### 邮箱服务体系
- 邮箱能力通过 `EmailServiceFactory` 注册，入口在 `src/services/__init__.py`。
- 当前实现包含：
  - `tempmail`：Tempmail.lol
  - `outlook`：Outlook OAuth / IMAP / Graph 相关能力
  - `custom_domain`：MoeMail 风格 REST API
  - `temp_mail`：自部署 Worker 风格临时邮箱
- 如果改邮箱服务逻辑，优先保持工厂注册模式和 `EmailServiceType` 分发，不要在注册引擎里写分支特判。

### 账号管理与导出/上传
- `src/web/routes/accounts.py` 负责账号列表、筛选、删除、批量操作、导出、token 读取等。
- 上传和外部系统对接分两层：
  - Web 路由：`src/web/routes/upload/*`
  - 实际上传实现：`src/core/upload/*`
- 已有外部目标包括 CPA、Sub2API、Team Manager；这些配置也存数据库表，而不是写死在文件里。

### 支付与订阅能力
- 支付相关路由在 `src/web/routes/payment.py`，核心实现位于 `src/core/openai/payment.py`。
- 账号模型中保留了 `cookies`、`subscription_type`、`subscription_at` 等字段，支付页和订阅检测依赖这些数据。
- README 说明了支付相关请求与上传行为对代理的不同策略；改这部分逻辑前先确认是否需要保持“某些上传直连、不走代理”的现有约定。

### 前端组织方式
- 页面模板在 `templates/*.html`，每个主页面通常对应一个 `static/js/*.js` 文件。
- 没有前端框架；交互逻辑直接调用 `/api/...` 和 WebSocket。
- 改页面时通常需要同时改：模板、对应 JS、以及对应后端路由返回结构。

## 关键文件
- `webui.py`：启动入口与运行目录初始化
- `src/web/app.py`：FastAPI 应用装配、页面路由、登录保护
- `src/web/routes/registration.py`：单次/批量注册任务编排
- `src/web/task_manager.py`：线程池、日志队列、WebSocket 推送
- `src/core/register.py`：核心注册引擎
- `src/services/`：邮箱服务实现与 Outlook 相关 provider
- `src/core/upload/`：CPA / Sub2API / Team Manager 上传实现
- `src/database/models.py`：持久化数据模型
- `src/config/settings.py`：数据库驱动的配置定义

## 修改时要注意的约定
- 打包运行和源码运行共存：涉及路径时先确认是否兼容 `sys.frozen` / `sys._MEIPASS` 分支，尤其是静态资源、模板、数据目录、日志目录。
- 不要把“实时任务状态”误当成数据库持久状态；很多进度信息只存在于 `TaskManager` 内存结构里。
- 如果要改任务/日志/批量状态推送，先读 `src/web/task_manager.py`，它已经专门处理了 WebSocket 重连后的历史日志补发与并发锁。
- 数据库没有 Alembic；SQLite 的轻量迁移逻辑写在 `DatabaseSessionManager.migrate_tables()`。新增持久字段时，通常需要同时更新 ORM 模型和这里的补列逻辑。
- 仓库当前 CI 只负责多平台 PyInstaller 打包，不会替你跑测试或 lint。