# ─────────────────────────────────────────────────────────────────────────────
#  EngFreq Makefile  —  常用命令：make help
# ─────────────────────────────────────────────────────────────────────────────

.PHONY: help setup start stop restart status logs update backup \
        docker docker-build docker-stop test clean

.DEFAULT_GOAL := help

help: ## 显示所有可用命令
	@echo ""
	@echo "  EngFreq — English Exam Word Analyzer"
	@echo ""
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / \
		{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@echo ""

# ── 初始化 ────────────────────────────────────────────────────────────────────

setup: ## 初始化环境（安装依赖、创建 venv）
	@bash start.sh --bootstrap

# ── 运行 ──────────────────────────────────────────────────────────────────────

start: ## 前台启动（开发模式，热重载）
	@bash start.sh

daemon: ## 后台启动（守护进程，日志写入 data/server.log）
	@bash deploy.sh --daemon

stop: ## 停止守护进程
	@bash deploy.sh --stop

restart: ## 重启守护进程（重装依赖）
	@bash deploy.sh --restart

status: ## 查看守护进程状态
	@bash deploy.sh --status

logs: ## 实时查看日志（Ctrl+C 退出）
	@tail -f data/server.log 2>/dev/null || echo "日志文件不存在，请先用 make daemon 启动"

# ── 快捷更新 ──────────────────────────────────────────────────────────────────

update: ## 零停机热更新：拉取最新代码并重启服务
	@echo ">>> 拉取最新代码..."
	@git pull --ff-only
	@echo ">>> 重启服务..."
	@bash deploy.sh --restart
	@echo ">>> 更新完成！"

update-docker: ## Docker 零停机热更新
	@echo ">>> 拉取最新代码..."
	@git pull --ff-only
	@echo ">>> 重新构建镜像..."
	@docker build -t engfreq .
	@echo ">>> 热更新容器（无停机）..."
	@docker compose -f docker-compose.prod.yml up -d --no-deps app
	@echo ">>> 更新完成！"

# ── Docker ────────────────────────────────────────────────────────────────────

docker: ## 构建镜像并以 Docker 启动
	@bash deploy.sh --docker

docker-build: ## 仅构建 Docker 镜像
	@docker build -t engfreq .

docker-stop: ## 停止 Docker 容器
	@docker compose down 2>/dev/null || docker compose -f docker-compose.prod.yml down 2>/dev/null || true

# ── 维护 ──────────────────────────────────────────────────────────────────────

backup: ## 备份数据库（data/app.db → data/backup/）
	@mkdir -p data/backup
	@cp data/app.db "data/backup/app_$(shell date +%Y%m%d_%H%M%S).db" 2>/dev/null || \
		cp app.db "data/backup/app_$(shell date +%Y%m%d_%H%M%S).db" 2>/dev/null || \
		echo "未找到数据库文件，跳过备份"
	@echo "备份完成"

health: ## 检查服务健康状态
	@bash deploy.sh --healthcheck

clean: ## 清理缓存和临时文件（不删数据库）
	@find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@find . -name "*.pyc" -delete 2>/dev/null || true
	@find . -name "*.pyo" -delete 2>/dev/null || true
	@echo "清理完成"
