from typing import Optional, List, Dict, Any
import logging

logger = logging.getLogger("mutsumi.pipeline")


class ModelPipeline:
    """模型 Pipeline - 支持 OpenAI 协议兼容提供商 + Tool 函数调用"""
    
    def __init__(
        self,
        provider: str = "openai",
        model: str = "gpt-4",
        temperature: float = 0.7,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        tools: List[Any] = None,
        **kwargs
    ):
        self.provider = provider
        self.model = model
        self.temperature = temperature
        self.api_key = api_key
        self.base_url = base_url
        self.extra_params = kwargs
        self._client = None
        self._llm = None
        self.tools = tools or []
        logger.info(f"Pipeline init: {provider} {model} base_url={base_url}, tools={len(self.tools)}")
    
    def _get_client(self):
        """获取 LLM 客户端"""
        if self._client is not None:
            return self._client
        
        logger.info(f"Creating client for {self.model}...")
        logger.info(f"  api_key: {'***' + self.api_key[-4:] if self.api_key else 'from env'}")
        logger.info(f"  base_url: {self.base_url or 'default'}")
        
        try:
            from langchain_openai import ChatOpenAI
            
            params = {
                "model": self.model,
                "temperature": self.temperature,
                **self.extra_params
            }
            
            if self.api_key:
                params["api_key"] = self.api_key
            
            if self.base_url:
                params["base_url"] = self.base_url
            
            logger.info(f"Creating ChatOpenAI with params: model={params.get('model')}, base_url={params.get('base_url', 'default')}")
            self._client = ChatOpenAI(**params)
            logger.info("Client created successfully")
        except Exception as e:
            logger.error(f"Failed to create client: {e}")
            self._client = None
        
        return self._client
    
    def _get_llm_with_tools(self):
        """获取绑定 Tools 的 LLM"""
        if self._llm is not None:
            return self._llm
        
        client = self._get_client()
        if client is None:
            return None
        
        logger.info(f"[TOOLS] self.tools = {self.tools}, len = {len(self.tools)}")
        
        if not self.tools:
            logger.warning("No tools available, using plain LLM")
            self._llm = client
            return self._llm
        
        try:
            from langchain_core.utils.function_calling import convert_to_openai_function
            
            functions = []
            for tool in self.tools:
                try:
                    func_dict = convert_to_openai_function(tool)
                    logger.info(f"[TOOLS] Converted: {func_dict.get('function', {}).get('name', 'unknown')}")
                    functions.append(func_dict)
                except Exception as e:
                    logger.error(f"[TOOLS] Failed to convert tool {tool.name}: {e}")
            
            if not functions:
                logger.warning("No functions converted, using plain LLM")
                self._llm = client
                return self._llm
            
            self._llm = client.bind(functions=functions)
            logger.info(f"LLM bound with {len(functions)} tools")
            
            logger.info(f"[TOOLS DEBUG] Bound functions: {functions}")
            
        except Exception as e:
            logger.error(f"Failed to bind tools: {e}")
            self._llm = client
        
        return self._llm
    
    async def chat(
        self,
        user_message: str,
        system_prompt: str = "",
        context: list[str] = None,
        max_tool_calls: int = 5
    ) -> str:
        """执行模型对话，支持 Tool 多轮调用"""
        logger.info(f"chat() called: user_message={user_message[:50]}...")
        
        from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, ToolMessage
        from langchain_core.utils.function_calling import convert_to_openai_function
        
        client = self._get_client()
        
        if client is None:
            logger.error("Client is None, returning error message")
            return "[模型客户端未配置]"
        
        messages = []
        
        if system_prompt:
            logger.info(f"Using system_prompt: {system_prompt[:100]}...")
            messages.append(SystemMessage(content=system_prompt))
        
        if context:
            context_str = "\n".join([f"用户: {c}" for c in context])
            logger.info(f"Context: {len(context)} messages")
            messages.append(HumanMessage(content=f"对话历史:\n{context_str}\n\n当前消息: {user_message}"))
        else:
            messages.append(HumanMessage(content=user_message))
        
        logger.info(f"Sending to LLM: {len(messages)} messages, tools={bool(self.tools)}")
        
        logger.info("\n" + "="*60)
        logger.info(">>> [PROMPT TO PROVIDER]")
        for i, msg in enumerate(messages):
            msg_type = type(msg).__name__
            content_preview = msg.content[:200] + "..." if len(msg.content) > 200 else msg.content
            logger.info(f"[{i}] {msg_type}: {content_preview}")
        logger.info("="*60)
        
        try:
            llm = self._get_llm_with_tools()
            
            for step in range(max_tool_calls):
                if step == 0:
                    current_messages = messages.copy()
                else:
                    current_messages = messages
                
                logger.info(f"[Step {step + 1}] Calling LLM...")
                logger.info(f"[TOOLS DEBUG] llm has tools: {hasattr(llm, 'bind')}")
                response = await llm.agenerate([current_messages])
                response_message = response.generations[0][0].message
                
                response_text = response_message.content
                tool_calls = response_message.tool_calls
                
                logger.info(f"[TOOLS DEBUG] response_message type: {type(response_message)}")
                logger.info(f"[TOOLS DEBUG] response_message.additional_kwargs: {response_message.additional_kwargs}")
                
                if not tool_calls and self.tools:
                    import re
                    import json
                    
                    patterns = [
                        (r'\{[\s\S]*"operation"[\s\S]*\}', lambda p: json.loads(p)),
                        (r'config_manager\s*\(\s*\{[\s\S]*\}\s*\)', lambda p: {
                            "operation": re.search(r'"operation"\s*:\s*"(\w+)"', p).group(1) if re.search(r'"operation"\s*:\s*"(\w+)"', p) else "get",
                            "key": re.search(r'"key"\s*:\s*"([^"]+)"', p).group(1) if re.search(r'"key"\s*:\s*"([^"]+)"', p) else ""
                        }),
                        (r'\{[\s\S]*"action"\s*:\s*"call_tool"[\s\S]*\}', lambda p: json.loads(p)),
                        (r'http_api_call\s*\(\s*\{[\s\S]*\}\s*\)', lambda p: {
                            "url": re.search(r'"url"\s*:\s*"([^"]+)"', p).group(1) if re.search(r'"url"\s*:\s*"([^"]+)"', p) else "",
                            "method": re.search(r'"method"\s*:\s*"(\w+)"', p).group(1) if re.search(r'"method"\s*:\s*"(\w+)"', p) else "GET"
                        }),
                    ]
                    
                    for pattern, parser in patterns:
                        json_match = re.search(pattern, response_text)
                        if json_match:
                            try:
                                parsed = parser(json_match.group())
                                logger.info(f"[TOOLS FALLBACK] Detected: {json_match.group()[:100]}...")
                                
                                if "action" in parsed and parsed.get("action") == "call_tool":
                                    tool_name = parsed.get("action_input", {}).get("tool_name", "")
                                    tool_args = parsed.get("action_input", {}).get("tool_arguments", {})
                                else:
                                    tool_name = parsed.get("name") or "config_manager"
                                    tool_args = parsed
                                
                                if "url" in tool_args:
                                    tool_name = "http_api_call"
                                
                                if not tool_name:
                                    tool_name = "config_manager"
                                
                                tool_calls = [{
                                    "name": tool_name,
                                    "args": tool_args,
                                    "id": f"call_{step}_fallback"
                                }]
                                response_message = AIMessage(
                                    content=response_text,
                                    tool_calls=tool_calls
                                )
                                logger.info(f"[TOOLS FALLBACK] Parsed tool: {tool_name}, args: {tool_args}")
                                break
                            except Exception as e:
                                logger.error(f"[TOOLS FALLBACK] Failed to parse: {e}")
                
                logger.info("\n" + "="*60)
                logger.info(f"<<< [RESPONSE FROM PROVIDER] Step {step + 1}")
                logger.info(f"Content: {response_text[:300]}..." if len(response_text) > 300 else f"Content: {response_text}")
                logger.info(f"Tool calls: {tool_calls}")
                logger.info("="*60)
                
                if not tool_calls:
                    logger.info(f"[Step {step + 1}] No tool calls, returning: {response_text[:50]}...")
                    return response_text
                
                logger.info(f"[Step {step + 1}] {len(tool_calls)} tool calls detected")
                
                messages.append(response_message)
                
                for tool_call in tool_calls:
                    tool_name = tool_call["name"]
                    tool_call_id = tool_call.get("id", f"call_{step}_{tool_name}")
                    tool_args = tool_call.get("args", {})
                    
                    logger.info(f"[TOOL] Calling: {tool_name} with args: {tool_args}")
                    
                    tool_result = ""
                    for tool in self.tools:
                        if tool.name == tool_name:
                            try:
                                result = tool.invoke(tool_args)
                                tool_result = str(result)
                                logger.info(f"[TOOL] {tool_name} result: {tool_result[:100]}...")
                            except Exception as e:
                                tool_result = f"[工具执行错误: {str(e)}]"
                                logger.error(f"[TOOL] {tool_name} error: {e}")
                            break
                    
                    if not tool_result:
                        tool_result = f"[未找到工具: {tool_name}]"
                    
                    messages.append(ToolMessage(content=tool_result, tool_call_id=tool_call_id))
                
                logger.info(f"[Step {step + 1}] Completed, continuing to next step...")
            
            logger.warning(f"Reached max tool calls ({max_tool_calls}), returning last response")
            return messages[-1].content if messages else "[达到最大调用次数]"
            
        except Exception as e:
            logger.error(f"Generate error: {e}")
            return f"[生成失败: {str(e)}]"