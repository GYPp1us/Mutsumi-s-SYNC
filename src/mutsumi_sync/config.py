import yaml
from pathlib import Path
from pydantic import BaseModel
from typing import Optional


class NapcatConfig(BaseModel):
    ws_url: str = "ws://localhost:3000"
    http_url: str = "http://localhost:3000"


class ModelConfig(BaseModel):
    provider: str = "openai"
    model: str = "gpt-4"
    temperature: float = 0.7


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
    context: ContextConfig = ContextConfig()
    memory: MemoryConfig = MemoryConfig()
    deduplication: DeduplicationConfig = DeduplicationConfig()
    cache: CacheConfig = CacheConfig()


def load_config(config_path: str = "config.yaml") -> Config:
    path = Path(config_path)
    if path.exists():
        with open(path) as f:
            data = yaml.safe_load(f)
            return Config(**data) if data else Config()
    return Config()
