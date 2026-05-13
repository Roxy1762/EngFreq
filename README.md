# English Exam Word Analyzer

一个基于 FastAPI 的英语试卷词汇分析工具。它可以上传 PDF、图片或文档，提取文本，分析词频与词形，并生成可导出的词汇表。

## 功能特点

- 用户注册、登录与管理员后台
- PDF / 图片 OCR 与文本提取
- 词频、词形、词族分析
- 多种词汇补全来源，可接 Claude、DeepSeek、OpenAI 兼容接口、词典 API
- CSV / XLSX 导出
- 试卷结果和词汇表分享码
- 一键服务器迁移：导出/导入完整数据快照（数据库 + 用户文件 + 词表）

## 项目结构

```text
backend/            FastAPI 后端与分析逻辑
frontend/           前端页面
data/wordlists/     内置词表数据
run.py              应用入口
start.bat           Windows 启动脚本
start.sh            Linux/macOS 启动脚本
requirements.txt    Python 依赖
```

## 环境要求

- Python 3.10 及以上
- Windows、Linux 或 macOS
- 如果要处理扫描件，建议安装 Tesseract OCR

## 快速开始

### Windows

```powershell
.\start.bat --bootstrap
.\start.bat
```

### Linux / macOS

```bash
chmod +x start.sh
./start.sh --bootstrap
./start.sh
```

### 直接运行

```bash
python run.py
python run.py --prod
```

默认访问地址为 `http://127.0.0.1:8000`。

## 配置

项目使用 `.env` 读取本地配置。首次启动前请先基于模板创建：

```bash
cp .env.example .env
```

Windows 也可以直接复制一份 `.env.example` 并改名为 `.env`。

常用配置项：

- `HOST` / `PORT`: 服务监听地址
- `ADMIN_USERNAME` / `ADMIN_PASSWORD`: 初始管理员账号
- `DB_PATH`: SQLite 数据库路径
- `SECRET_KEY`: JWT 签名密钥
- `VOCAB_PROVIDER`: 默认词汇补全来源
- `TESSERACT_CMD`: Windows 下 Tesseract 可执行文件路径

`.env`、`secret.key`、数据库文件和运行生成的数据目录都已被 `.gitignore` 排除，不会被提交到 GitHub。

## GitHub 上传建议

这个仓库已经按常见 GitHub 项目习惯整理：

- 已补充 `.gitignore`
- 已补充 `README.md`
- 已统一文本文件换行规则
- 已排除本地密钥、数据库、缓存、虚拟环境和上传结果目录

如果你准备首次推送：

```bash
git init
git add .
git commit -m "Initial commit"
```

然后在 GitHub 新建仓库后执行：

```bash
git remote add origin <your-repo-url>
git branch -M main
git push -u origin main
```

## 服务器一键迁移

整台服务器（数据库 + 用户上传文件 + 词表 + 可选的 OCR 缓存）可以被打包成一个 `.zip`，在新机器上一键还原。

### 通过管理后台（推荐）

1. 在源服务器登录 `/admin-panel`，进入「数据迁移」标签
2. 勾选要包含的内容（用户文件、词表、OCR 缓存），点击 **生成并下载快照**
3. 在目标服务器上完成基本部署（同样的 `start.sh` 或 `docker compose up`）并以管理员身份登录
4. 进入「数据迁移」，选择刚才下载的 `.zip`
5. 建议先点 **试运行** 校验，再点 **立即导入**

导入会自动把目标服务器当前状态备份到 `data/migration_backups/`，可在同一界面下载或删除。

### 通过命令行

源机器：

```bash
./deploy.sh --migrate-export ./snapshot.zip
```

目标机器（部署后启动）：

```bash
./deploy.sh --migrate-import ./snapshot.zip
```

两个命令都需要 `.env` 或环境变量提供 `ADMIN_USERNAME` / `ADMIN_PASSWORD`，并要求服务正在运行。

### 直接调用 API

```bash
# 取 token
TOKEN=$(curl -s -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"yourpw"}' \
  http://host:8000/auth/login | jq -r .token)

# 导出
curl -L -H "Authorization: Bearer $TOKEN" \
  'http://host:8000/admin/migration/export?include_file_store=true' \
  -o snapshot.zip

# 导入（先试运行）
curl -H "Authorization: Bearer $TOKEN" \
  -F "file=@snapshot.zip" -F "dry_run=true" \
  http://host2:8000/admin/migration/import
```

迁移包结构：

```
manifest.json             # 版本、计数、SHA-256 校验
db/app.db                 # 通过 SQLite Backup API 生成的一致性快照
data/files/...            # 持久化的上传文件副本
data/wordlists/...        # 词表（可选）
data/ocr_cache/...        # OCR 缓存（可选，默认关闭）
```

## 注意事项

- 不要把真实 `.env` 提交到仓库
- 不要把 `app.db`、`secret.key`、`.venv/` 上传到 GitHub
- 生产环境请务必修改默认管理员密码
- 迁移导入会替换数据库，请始终先做 **试运行**（系统也会自动生成回滚快照）

