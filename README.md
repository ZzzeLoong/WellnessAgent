# WellnessAgent

一个面向饮食规划与健康管理场景的 Agent Demo，包含：

- Python 后端：`FastAPI`
- 前端：`React + Vite`
- 记忆系统：短期 `working memory` + 长期 `episodic memory`
- 检索增强：`RAG + Qdrant`

## 1. 环境准备

建议使用 Python `3.10+` 和 Node.js `18+`。

先创建并激活虚拟环境：

```powershell
python -m venv .venv
.\.venv\Scripts\activate
```

安装 Python 依赖：

```powershell
pip install -r requirements.txt
```

安装前端依赖：

```powershell
cd frontend
npm install
cd ..
```

## 2. 配置项目

复制一份环境变量模板：

```powershell
copy .env.example .env
```

然后至少配置这些字段：

- `LLM_MODEL_ID`
- `LLM_API_KEY`
- `LLM_BASE_URL`
- `EMBED_MODEL_TYPE`
- `EMBED_MODEL_NAME`
- `QDRANT_URL`

常见本地配置示例：

```env
LLM_MODEL_ID=qwen3:8b
LLM_API_KEY=ollama
LLM_BASE_URL=http://localhost:11434/v1

EMBED_MODEL_TYPE=local
EMBED_MODEL_NAME=sentence-transformers/all-MiniLM-L6-v2

QDRANT_URL=http://localhost:6333
QDRANT_API_KEY=
QDRANT_COLLECTION=hello_agents_vectors
QDRANT_DISTANCE=cosine
```

如果你切换了 `EMBED_MODEL_TYPE` 或 `EMBED_MODEL_NAME`，向量维度可能会变化。RAG 集合会自动按需重建，但已有历史向量数据仍建议谨慎处理。

## 3. 启动 Qdrant

本地最简单的方式是用 Docker：

```powershell
docker run -p 6333:6333 -p 6334:6334 qdrant/qdrant
```

## 4. 启动后端

```powershell
.\.venv\Scripts\uvicorn wellnessagent.server.main:app --reload
```

默认地址：

- API: `http://127.0.0.1:8000`
- 健康检查: `http://127.0.0.1:8000/api/health`

## 5. 启动前端

```powershell
cd frontend
npm run dev
```

默认地址：

- 前端: `http://127.0.0.1:5173`

## 6. 运行 Demo

如果你想直接跑命令行 demo：

```powershell
.\.venv\Scripts\python.exe wellnessagent\demo.py
```

## 7. 基于 hello_agents

本项目基于 `hello_agents` 项目的部分代码与设计思路进行开发，并在此基础上实现了面向饮食规划与健康管理场景的 Agent、记忆系统、RAG 流程以及前后端调试界面。

原项目许可证为 `Attribution-NonCommercial-ShareAlike 4.0 International` (`CC BY-NC-SA 4.0`)。

如果你继续分发、修改或复用本项目中的相关衍生部分，请同时注意遵守上游项目许可证的署名、非商业使用和相同方式共享要求。
