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
