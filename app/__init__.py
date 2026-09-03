"""FastAPI 登录注册 + 工具箱后端应用包。

目录结构（按职责/功能分层）：
  core/    数据库连接、安全与认证依赖
  models/  SQLAlchemy ORM 模型
  schemas/ Pydantic 请求/响应模型
  routers/ 每个功能一个路由文件（auth / futures / positions / watchlist）
  main.py  应用入口：建 app、页面路由、静态挂载、include 各 router
"""
