import asyncio
import os

os.environ["OPENAI_API_KEY"] = "YOUR_DEEPSEEK_API_KEY"

import sys
sys.path.insert(0, "/home/ubuntu/gits/mutsumi-sync")

from src.mutsumi_sync.processor.pipeline import ModelPipeline
from src.mutsumi_sync.processor.tools import config_manager, http_api_call


async def test_tool_calling():
    print("=== 测试 Tool 调用功能 ===\n")
    
    tools = [config_manager, http_api_call]
    
    print("=== 使用 deepseek-chat ===")
    pipeline = ModelPipeline(
        provider="deepseek",
        model="deepseek-chat",
        temperature=0.7,
        api_key="YOUR_DEEPSEEK_API_KEY",
        base_url="https://api.deepseek.com/",
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