zsy12345 · 纯静态工具箱
========================

## 是什么
8 个纯前端小工具，零后端、零构建、零依赖。直接双击 index.html 就能用。

## 工具
时间戳转换 · 哈希值计算 · 汇率转换（默认新台币）· 俄罗斯方块
灵签占卜 · 简繁转换 · 笔记本 · 定价模拟器

## 本地预览
直接双击 index.html；或用任意静态服务器（推荐，避免个别浏览器对 file:// 的限制）：

    python3 -m http.server 8642
    # 然后打开 http://127.0.0.1:8642

## 服务器部署（nginx）
    root /path/to/zsy12345;
    index index.html;
    # 路径已全部相对化，放在子路径（如 /tools/）也能正常工作，无需额外配置

## 目录
    index.html          首页工具箱（4 列 × 4 行 = 16 格：8 工具 + 8 占位）
    favicon.svg
    modules/
      <工具名>/
        <工具名>.html    入口
        data/            笔记本的 markdown 与素材
      lib/terminal.js    终端风格 UI 公共脚本 + marked/opencc/xlsx 三个第三方库

## 新增工具
在 modules/ 下建目录 + 放同名 .html，在 index.html 加一张卡片即可，无需改任何配置。
注意：格子总数须为 4 的整数倍（当前 16），否则末行留空。

## 历史
2026-10-16 起移除 FastAPI 后端（原登录注册 + 期货行情看板）与全部数据库表，
项目转为纯静态。代码仍可从 git 历史取回：
    git show 0118db6:app/main.py            # 后端入口
    git show ff40f65:requirements.txt      # 依赖清单