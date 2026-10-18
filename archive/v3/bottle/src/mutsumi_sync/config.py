import yaml
import json
from pathlib import Path
from pydantic import BaseModel
from typing import Optional, Any


class NapcatConfig(BaseModel):
    ws_url: str = "ws://localhost:3000"
    http_url: str = "http://localhost:3000"
    access_token: Optional[str] = None


class ModelConfig(BaseModel):
    provider: str = "openai"
    model: str = "gpt-4"
    temperature: float = 0.7
    api_key: Optional[str] = None
    base_url: Optional[str] = None


class ContextConfig(BaseModel):
    window_size: int = 20
    max_tokens: int = 4096


class MemoryConfig(BaseModel):
    pg_connection: str = "postgresql://user:pass@localhost:5432/mutsumi"
    vector_dim: int = 1536


class DeduplicationConfig(BaseModel):
    wait_time: float = 1.0


class CacheConfig(BaseModel):
    image_md5: str = "./cache/image_md5.json"
    meme_desc: str = "./cache/meme_desc.json"


class Config(BaseModel):
    napcat: NapcatConfig = NapcatConfig()
    model: ModelConfig = ModelConfig()
    system_prompt: str = ""
    context: ContextConfig = ContextConfig()
    memory: MemoryConfig = MemoryConfig()
    deduplication: DeduplicationConfig = DeduplicationConfig()
    cache: CacheConfig = CacheConfig()
    _config_path: Optional[str] = None

    def save(self):
        if self._config_path:
            with open(self._config_path, 'w', encoding='utf-8') as f:
                yaml.dump(self.model_dump(exclude_none=True), f, allow_unicode=True, default_flow_style=False)

    def reload(self):
        if self._config_path and Path(self._config_path).exists():
            with open(self._config_path, encoding='utf-8') as f:
                data = yaml.safe_load(f)
                if data:
                    for key, value in data.items():
                        if hasattr(self, key):
                            if isinstance(value, dict):
                                obj = getattr(self, key)
                                for k, v in value.items():
                                    if hasattr(obj, k):
                                        setattr(obj, k, v)
                            else:
                                setattr(self, key, value)


_config_instance: Optional[Config] = None


def load_config(config_path: str = None) -> Config:
    global _config_instance
    
    if config_path is None:
        config_path = "config.yaml"
    
    path = Path(config_path)
    if not path.exists():
        path = Path.cwd() / config_path
    
    if path.exists():
        with open(path, encoding='utf-8') as f:
            data = yaml.safe_load(f)
            _config_instance = Config(**data) if data else Config()
            _config_instance._config_path = str(path)
    else:
        _config_instance = Config()
        _config_instance._config_path = str(path)
    
    return _config_instance


def get_config() -> Config:
    return _config_instance