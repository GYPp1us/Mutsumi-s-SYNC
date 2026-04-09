from typing import Optional


class ModelPipeline:
    def __init__(
        self,
        model_name: str = "gpt-4",
        temperature: float = 0.7,
        api_key: Optional[str] = None
    ):
        self.model_name = model_name
        self.temperature = temperature
        self.api_key = api_key
        self._client = None

    def _get_client(self):
        """获取 LLM 客户端"""
        if self._client is None:
            try:
                from langchain_openai import ChatOpenAI
                self._client = ChatOpenAI(
                    model=self.model_name,
                    temperature=self.temperature,
                    api_key=self.api_key
                )
            except ImportError:
                self._client = None
        return self._client

    async def chat(
        self,
        user_message: str,
        system_prompt: str = "",
        context: list[str] = None
    ) -> str:
        """执行模型对话"""
        client = self._get_client()
        
        if client is None:
            return "[模型客户端未配置]"
        
        from langchain.schema import HumanMessage, SystemMessage
        
        messages = []
        
        if system_prompt:
            messages.append(SystemMessage(content=system_prompt))
        
        if context:
            context_str = "\n".join([f"用户: {c}" for c in context])
            messages.append(HumanMessage(content=f"对话历史:\n{context_str}\n\n当前消息: {user_message}"))
        else:
            messages.append(HumanMessage(content=user_message))
        
        response = await self._client.agenerate([messages])
        return response.generations[0][0].text
