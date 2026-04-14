from langchain.tools import tool
from typing import Optional, Dict, Any
from ..config import get_config
import httpx
import json
import logging
import asyncio

logger = logging.getLogger("mutsumi.tools")


@tool
def config_manager(operation: str, key: str = "", value: Any = None) -> str:
    """
    管理 Mutsumi's SYNC 配置文件。
    
    操作:
    - "get": 获取配置项值
    - "set": 设置配置项值（需要 key 和 value）
    - "list": 列出所有配置项
    - "reload": 重新加载配置文件
    
    参数:
    - operation: 操作类型 (get/set/list/reload)
    - key: 配置项路径，如 "model.temperature" 或 "napcat.ws_url"
    - value: 设置值（仅 set 操作需要）
    
    返回: 操作结果
    """
    config = get_config()
    
    if config is None:
        return "[配置未初始化，请先启动机器人]"
    
    try:
        if operation == "list":
            result = {}
            for field in ["napcat", "model", "context", "memory", "deduplication", "cache", "system_prompt"]:
                if hasattr(config, field):
                    val = getattr(config, field)
                    result[field] = val.model_dump() if hasattr(val, 'model_dump') else str(val)
            return json.dumps(result, indent=2, ensure_ascii=False)
        
        if operation == "reload":
            config.reload()
            return "配置已重新加载"
        
        if operation == "get":
            if not key:
                return "请指定 key 参数"
            parts = key.split(".")
            obj = config
            for part in parts:
                if hasattr(obj, part):
                    obj = getattr(obj, part)
                else:
                    return f"配置项 {key} 不存在"
            return str(obj)
        
        if operation == "set":
            if not key or value is None:
                return "set 操作需要 key 和 value 参数"
            parts = key.split(".")
            obj = config
            for part in parts[:-1]:
                if hasattr(obj, part):
                    obj = getattr(obj, part)
                else:
                    return f"配置项路径 {key} 不存在"
            
            if hasattr(obj, parts[-1]):
                setattr(obj, parts[-1], value)
                config.save()
                return f"已设置 {key} = {value}"
            else:
                return f"配置项 {key} 不存在"
    except Exception as e:
        return f"[配置操作错误: {str(e)}]"


@tool
def http_api_call(url: str, method: str = "GET", headers: Dict = None, data: Any = None, json_data: Any = None, timeout: int = 10) -> str:
    """
    调用外部 HTTP API。
    
    参数:
    - url: 请求 URL
    - method: HTTP 方法 (GET/POST/PUT/DELETE)
    - headers: 请求头（字典）
    - data: 表单数据（字典）
    - json_data: JSON 请求体（字典）
    - timeout: 超时秒数（默认 10）
    
    返回: 响应内容或错误信息
    """
    try:
        loop = asyncio.get_running_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(_sync_http_call, url, method, headers, data, json_data, timeout)
                result = future.result(timeout=timeout + 5)
                return json.dumps(result, ensure_ascii=False)
    except RuntimeError:
        pass
    
    try:
        result = asyncio.run(_call())
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        logger.error(f"[TOOL] http_api_call error: {e}")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


def _sync_http_call(url: str, method: str, headers: Dict, data: Any, json_data: Any, timeout: int) -> dict:
    """同步 HTTP 调用（在线程池中执行）"""
    import httpx
    try:
        with httpx.Client(timeout=timeout) as client:
            kwargs = {"url": url}
            
            if headers:
                kwargs["headers"] = headers
            
            if method.upper() == "GET":
                kwargs["params"] = data
            elif method.upper() in ("POST", "PUT", "DELETE"):
                if json_data:
                    kwargs["json"] = json_data
                else:
                    kwargs["data"] = data
            
            resp = client.request(method.upper(), **kwargs)
            return {
                "status": resp.status_code,
                "body": resp.text[:2000]
            }
    except Exception as e:
        return {"error": str(e)}