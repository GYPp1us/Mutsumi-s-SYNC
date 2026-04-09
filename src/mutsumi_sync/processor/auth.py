from enum import Enum
from typing import Set


class Role(Enum):
    ADMIN = "admin"
    USER = "user"


class AuthManager:
    def __init__(self):
        self._admins: Set[int] = set()

    def add_admin(self, user_id: int):
        self._admins.add(user_id)

    def remove_admin(self, user_id: int):
        self._admins.discard(user_id)

    def is_admin(self, user_id: int) -> bool:
        return user_id in self._admins

    def get_role(self, user_id: int) -> Role:
        return Role.ADMIN if self.is_admin(user_id) else Role.USER
