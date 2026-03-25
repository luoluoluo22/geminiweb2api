# GeminiWeb2API - Gemini Web 转 OpenAI API 代理

这是一个强大的 Gemini Web 转 OpenAI API 代理服务，支持多账号管理、自动 Cookie 同步、多模态对话和图片生成。

## ✨ 核心特性

- **🤖 OpenAI 兼容**: 完美兼容 OpenAI `/v1/chat/completions` 和 `/v1/models` 接口。
- **🍪 多账号自动轮询**: 支持添加多个 Google 账号，请求时自动轮询使用，提高并发能力。
- **🔌 自动化 Cookie 同步**: 
  - 提供配套 **Chrome 扩展**。
  - 自动在浏览器后台打开/刷新 Gemini 页面，提取最新 Cookie 并同步到服务器。
  - 彻底解决 Cookie 过期问题，实现由“人工维护”到“全自动托管”。
- **🖼️ 强大的多模态支持**:
  - **文生图**: 支持调用 Gemini 的绘图能力。
  - **图生文/图生图**: 支持上传图片进行多模态对话和参考图修改。
- **💾 本地化存储**: 生成的图片自动下载并缓存到本地，支持 URL 或 Base64 两种返回模式。
- **🛡️ 安全设计**: 
  - 管理面板独立密码保护。
  - 插件连接使用专用 Token，与 API Key 分离，保障安全。
- **🐳 极速部署**: 提供 Docker 镜像，一键启动。

---

## 🚀 快速部署

### 方式一：Docker (推荐)

直接使用 Docker Hub 镜像启动：

```bash
docker run -d \
  --name geminiweb2api \
  -p 8000:8000 \
  -v $(pwd)/data:/app/data \
  -e TZ=Asia/Shanghai \
  lumia1998/geminiweb2api:latest
```

或者使用 `docker-compose.yml`:

```yaml
version: '3'
services:
  geminiweb2api:
    image: lumia1998/geminiweb2api:latest
    container_name: geminiweb2api
    ports:
      - "8000:8000"
    volumes:
      - ./data:/app/data
      - ./images:/app/geminiweb2api/static/images
    environment:
      - TZ=Asia/Shanghai
    restart: always
```

访问管理面板: `http://localhost:8000/admin` (默认账号: `admin` / `admin`)

### 方式二：Python 源码运行

```bash
# 克隆仓库
git clone https://github.com/lumia1998/geminiweb2api.git
cd geminiweb2api

# 安装依赖
pip install -r requirements.txt

# 启动服务
python main.py
```

### 方式三：Vercel 一键部署

[![Deploy with Vercel](https://vercel.com/button)](https://vercel.com/new/clone?repository-url=https://github.com/luoluoluo22/geminiweb2api)

仓库已包含 `api/index.py`、`vercel.json` 和 `.vercelignore`，推送到 GitHub 后可直接导入到 Vercel。

首次部署建议在 Vercel Project Settings -> Environment Variables 中至少配置：

- `UPSTASH_REDIS_URL`: 推荐直接填写 `redis://default:password@host:6379` 这种连接串，最直观
- `UPSTASH_REDIS_REST_URL`: 推荐填写带 Token 的完整 Upstash REST URL，例如 `https://xxx-xxxxx.upstash.io?_token=xxxxx`
- `BOOTSTRAP_COOKIE_STRING`: 完整 Gemini Cookie 字符串，至少要包含 `__Secure-1PSID`，最好同时包含 `__Secure-1PSIDTS`
- `BOOTSTRAP_API_KEY`: 对外提供给 OpenAI SDK 的 API Key
- `BOOTSTRAP_ADMIN_USERNAME`: 后台用户名
- `BOOTSTRAP_ADMIN_PASSWORD`: 后台密码

可选环境变量：

- `UPSTASH_REDIS_KEY`: Redis 中使用的键名，默认 `geminiweb2api:data`
- `BOOTSTRAP_IMAGE_MODE`: `url` 或 `base64`，Vercel 上如果设置为 `url` 会自动降级为 `base64`
- `BOOTSTRAP_PROXY_URL`: 代理地址
- `BOOTSTRAP_TIMEOUT`: 请求超时秒数

推荐部署方式：

1.  在 Upstash 创建 Redis 数据库。
2.  直接复制 `redis://default:password@host:6379` 连接串。
3.  在 Vercel 只创建一个环境变量 `UPSTASH_REDIS_URL`，值就是上面的完整连接串。
4.  再填写 `BOOTSTRAP_COOKIE_STRING`、`BOOTSTRAP_API_KEY` 等初始化变量后部署。

如果你更习惯 REST 方式，也可以继续使用 `UPSTASH_REDIS_REST_URL`。

Upstash 创建步骤：

1.  登录 Upstash 控制台并新建一个 Redis Database。
2.  在数据库详情页找到 `Details` 或连接信息区域。
3.  复制 `redis://default:password@host:6379` 连接串。
4.  把这整串填到 Vercel 的 `UPSTASH_REDIS_URL` 环境变量。
5.  如果你不用 `redis://`，也可以到 `REST API` 区域复制 `Endpoint` 和 `Token`，拼成 `UPSTASH_REDIS_REST_URL`。

说明：

- 项目现在会优先使用 Upstash Redis 持久化 `cookies.json` 中的全部内容，包括 Cookie 池、管理员配置、API Key、插件 Token 和保活状态。
- 如果同时配置了 `UPSTASH_REDIS_URL` 和 `UPSTASH_REDIS_REST_URL`，会优先使用 `UPSTASH_REDIS_URL`。
- 如果配置了 `UPSTASH_REDIS_REST_URL`，即使 Vercel 实例重启，数据也不会随着 `/tmp` 一起丢失。
- 如果 `UPSTASH_REDIS_REST_URL` 本身已经带 `_token=...`，就不需要额外再配置 `UPSTASH_REDIS_REST_TOKEN`。

注意事项：

- Vercel 是 Serverless 环境，没有稳定的本地持久化磁盘，也不适合长期后台循环。
- 当前实现会在 Vercel 上自动关闭后台保活循环，改为“每次请求前按需保活”。
- 如果没有接入 Upstash，`cookies.json` 和图片缓存会落在 `/tmp`，实例回收后可能丢失。
- 图片缓存本身仍然是临时文件，不建议把 Vercel 当成长期图片托管。

---

## ⚙️ 配置指南

### 1. 初始化设置

1.  访问管理面板 `http://localhost:8000/admin`。
2.  输入默认账号密码登录。
3.  在 **Setting 配置** 页面修改默认密码和设置 API Key (用于客户端调用)。

### 2. 添加 Cookie (自动化方式 - 推荐)

为了保持 Cookie 长期有效，建议安装配套的浏览器扩展实现自动同步：

1.  访问 **[Gemini Cookie Auto Sync 项目](https://github.com/lumia1998/gemini2webapi_cookie_auto)**。
2.  按照该项目的说明下载并安装 Chrome 扩展。
3.  在扩展设置中填入管理面板显示的 **服务器地址** 和 **连接 Token**。
4.  在浏览器登录 Gemini 官网。
5.  扩展会自动提取 Cookie 并同步到您的服务器，且后续会自动在后台保活。

### 3. 添加 Cookie (手动方式)

1.  登录 https://gemini.google.com。
2.  F12 打开开发者工具 -> Application -> Cookies。
3.  右键 `https://gemini.google.com` 的任意 Cookie -> "Copy all as Header String"。
4.  在管理面板点击 **新增**，粘贴完整 Cookie 字符串。

---

## 📡 API 调用示例

### OpenAI Python SDK

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8000/v1",
    api_key="你在后台设置的API_KEY"
)

# 1. 文本对话
response = client.chat.completions.create(
    model="gemini-3.0-pro",
    messages=[{"role": "user", "content": "你好"}]
)
print(response.choices[0].message.content)

# 2. 图片生成 (Gemini 会返回图片链接)
response = client.chat.completions.create(
    model="gemini-3.0-pro",
    messages=[{"role": "user", "content": "画一只戴墨镜的猫"}]
)
print(response.choices[0].message.content)
# 输出示例: 
# 好的，这是一只戴墨镜的猫：
# ![Generated Image](http://localhost:8000/static/images/img_xxxx.png)

# 3. 多模态 (图片理解/参考图生成)
# 需要将图片转为 Base64 或使用 URL
response = client.chat.completions.create(
    model="gemini-3.0-pro",
    messages=[{
        "role": "user",
        "content": [
            {"type": "text", "text": "这张图里有什么？"},
            {"type": "image_url", "image_url": {"url": "https://example.com/image.jpg"}}
        ]
    }]
)
print(response.choices[0].message.content)
```

### 支持的模型

- `gemini-3.0-flash`
- `gemini-3.0-pro`
- `gemini-3.0-flash-thinking`

---

## 🛠️ 目录结构

```
/app
├── data/              # 数据持久化目录
│   └── cookies.json   # 存储 Cookie 和配置
├── geminiweb2api/     # 核心代码
│   ├── static/        # 静态资源 (含生成的图片)
│   ├── templates/     # 前端模板
│   └── ...
└── main.py            # 入口文件
```

## 📝 常见问题

**Q: 为什么生成的图片链接无法访问？**
A: 请确保在后台设置中配置了正确的 `Base URL` (例如 `http://your-ip:8000`)。如果使用 Docker，确保端口映射正确。

**Q: Cookie 多久过期？**
A: Google 的 Cookie 有效期较短。强烈建议配合油猴脚本/插件使用，它会自动检测并更新 Cookie，实现无人值守。

**Q: 如何重置管理员密码？**
A: 停止服务，手动编辑 `data/cookies.json` 文件，修改 `admin_password` 字段，或直接删除该文件重置所有配置。

## ⚠️ 免责声明

本项目仅供学习和研究使用。用户应遵守 Google 的服务条款。开发者不对使用本项目产生的任何后果负责。
