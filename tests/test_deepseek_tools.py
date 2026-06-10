import asyncio
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mutsumi_sync.processor.pipeline import ModelPipeline
from mutsumi_sync.processor.tools import config_manager, http_api_call

API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/")


async def test_tool_calling():
    print("=== 测试 Tool 调用功能 ===\n")
    
    if not API_KEY:
        print("请设置环境变量 DEEPSEEK_API_KEY")
        return
    
    tools = [config_manager, http_api_call]
    
    print("=== 使用 deepseek-chat ===")
    pipeline = ModelPipeline(
        provider="deepseek",
        model="deepseek-chat",
        temperature=0.7,
        api_key=API_KEY,
        base_url=BASE_URL,
        tools=tools
    )
    
    print("1. 测试 config_manager...")
    result = await pipeline.chat(
        "获取 model.model 的值",
        system_prompt="你需要获取配置时，调用 config_manager 工具，参数 operation=get, key='model.model'",
        max_tool_calls=3
    )
    print(f"结果: {result[:300]}...")
    print()
    
    print("2. 测试 http_api_call...")
    result2 = await pipeline.chat(
        "调用 https://httpbin.org/get",
        system_prompt="你需要调用外部API时，使用 http_api_call，参数 url='https://httpbin.org/get', method='GET'",
        max_tool_calls=3
    )
    print(f"结果: {result2[:300]}...")
    print()
    
    print("=== 测试完成 ===")


if __name__ == "__main__":
    asyncio.run(test_tool_calling())