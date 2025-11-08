import mysql.connector

class DatabaseConnection:
    def __init__(self, connection: mysql.connector.MySQLConnection):
        self.connection = connection
        self.cursor = None
        self.nesting_level = 0

    def __enter__(self):
        if self.nesting_level == 0:
            self.connection.ping(reconnect=True, attempts=10, delay=1)
            self.cursor = self.connection.cursor(dictionary=True)
        self.nesting_level += 1
        return self.cursor

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.nesting_level -= 1
        if self.nesting_level == 0:
            try:
                if exc_type:
                    self.connection.rollback()
                else:
                    self.connection.commit()
            finally:
                if self.cursor:
                    self.cursor.close()
                    self.cursor = None
        elif self.nesting_level < 0:
            self.nesting_level = 0
