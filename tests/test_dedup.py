import pytest
import asyncio
from src.mutsumi_sync.processor.dedup import Deduplicator
from src.mutsumi_sync.processor.auth import AuthManager, Role


@pytest.mark.asyncio
async def test_dedup_cancel():
    dedup = Deduplicator(wait_time=0.1)
    
    assert await dedup.should_reply(123, 456)
    
    await dedup.schedule_reply(123, 456)
    
    assert await dedup.should_reply(123, 456)


def test_auth_manager():
    auth = AuthManager()
    auth.add_admin(123)
    assert auth.is_admin(123)
    assert auth.get_role(123) == Role.ADMIN
    assert auth.get_role(456) == Role.USER
