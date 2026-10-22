"""Use only the manager's existing SQLAlchemy maintenance transaction."""
from alembic import context

connection = context.config.attributes.get("connection")
if connection is None or not connection.in_transaction():
    raise RuntimeError("Alembic requires the manager's active Core transaction")
context.configure(connection=connection, transactional_ddl=True, transaction_per_migration=False)
with context.begin_transaction():
    context.run_migrations()
