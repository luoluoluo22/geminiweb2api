import os
from openai import OpenAI

# 默认设置中 api_key 是 "sk-123456"，如果在后台修改过请替换
API_KEY = "sk-123456"
# 服务地址
BASE_URL = "https://gemini.luoluoluo.cc.cd/v1"

client = OpenAI(
    api_key=API_KEY,
    base_url=BASE_URL,
)

print("正在发送请求到 GeminiProxy...")
try:
    response = client.chat.completions.create(
        model="gemini-3.0-pro",
        messages=[{"role": "user", "content": "你好，请用一句话介绍自己。"}],
        stream=True
    )

    print("Gemini响应: ", end="", flush=True)
    for chunk in response:
        content = chunk.choices[0].delta.content
        if content:
            print(content, end="", flush=True)
    print("\n")
except Exception as e:
    print(f"请求失败: {e}")
