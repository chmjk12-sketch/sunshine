# Agent 开发规范

## 项目结构
- `app/main.py` - FastAPI 入口
- `templates/` - Jinja2 模板
- `agent.yaml` - Agent 元数据

## 代码规范
- 使用 Pydantic v2 进行数据验证
- 所有端点返回标准格式: `{code, data, message}`
- 数据库操作使用上下文管理器
- 异常必须捕获并返回友好错误信息

## 部署规范
- 内部端口必须为 80
- 必须提供 /health 端点
- 必须提供 /metrics 端点
- Docker 镜像必须包含 HEALTHCHECK
