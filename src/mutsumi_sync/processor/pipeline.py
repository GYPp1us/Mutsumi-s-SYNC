from typing import Optional


class ModelPipeline:
    """模型 Pipeline - 支持 OpenAI 协议兼容提供商"""
    
    def __init__(
        self,
        provider: str = "openai",
        model: str = "gpt-4",
        temperature: float = 0.7,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        **kwargs
    ):
        self.provider = provider
        self.model = model
        self.temperature = temperature
        self.api_key = api_key
        self.base_url = base_url
        self.extra_params = kwargs
        self._client = None
    
    def _get_client(self):
        """获取 LLM 客户端"""
        if self._client is not None:
            return self._client
        
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
            
            self._client = ChatOpenAI(**params)
        except Exception as e:
            print(f"Failed to create client: {e}")
            self._client = None
        
        return self._client
    
    async def chat(
        self,
        user_message: str,
        system_prompt: str = "",
        context: list[str] = None
    ) -> str:
        """执行模型对话"""
        from langchain.schema import SystemMessage, HumanMessage
        
        client = self._get_client()
        
        if client is None:
            return "[模型客户端未配置]"
        
        messages = []
        
        if system_prompt:
            messages.append(SystemMessage(content=system_prompt))
        
        if context:
            context_str = "\n".join([f"用户: {c}" for c in context])
            messages.append(HumanMessage(content=f"对话历史:\n{context_str}\n\n当前消息: {user_message}"))
        else:
            messages.append(HumanMessage(content=user_message))
        
        try:
            response = await client.agenerate([messages])
            return response.generations[0][0].text
        except Exception as e:
            return f"[生成失败: {str(e)}]"