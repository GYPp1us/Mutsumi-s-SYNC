from typing import Optional


class PostgresMemory:
    def __init__(self, connection_string: str):
        self.conn_string = connection_string
        self._conn = None
        self._connected = False

    def connect(self):
        try:
            import psycopg2
            self._conn = psycopg2.connect(self.conn_string)
            self._connected = True
        except ImportError:
            print("psycopg2 not installed, using in-memory storage")
            self._connected = False
            self._messages = []

    def save_message(self, user_id: int, group_id: Optional[int], message: str, role: str):
        if not self._connected:
            self._messages.append({
                "user_id": user_id,
                "group_id": group_id,
                "message": message,
                "role": role
            })
            return
        
        cursor = self._conn.cursor()
        cursor.execute(
            "INSERT INTO messages (user_id, group_id, message, role) VALUES (%s, %s, %s, %s)",
            (user_id, group_id, message, role)
        )
        self._conn.commit()

    def close(self):
        if self._conn:
            self._conn.close()
