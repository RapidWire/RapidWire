import mysql.connector

class DatabaseConnection:
    def __init__(self, connection: mysql.connector.MySQLConnection):
        self.connection = connection
        self.cursor = None

    def __enter__(self):
        self.connection.ping(reconnect=True, attempts=5, delay=3)
        self.cursor = self.connection.cursor(dictionary=True)
        return self.cursor

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self.connection.rollback()
        else:
            self.connection.commit()
        if self.cursor:
            self.cursor.close()
