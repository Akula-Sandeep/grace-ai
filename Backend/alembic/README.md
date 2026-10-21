PostgreSQL is the development, test-integration, and deployment database. The default local URL targets `localhost:5432`; set `DATABASE_URL` to your PostgreSQL instance before running the API or migrations. Schema changes are Alembic-owned: the application does not create tables on startup. Run `alembic upgrade head` to prepare a database.

The SQLite engines used by unit tests are explicit in-memory or temporary test fixtures. They are not a supported application or migration target, and `init_db()` intentionally raises rather than creating a divergent schema.

To verify an empty install, an upgrade from the previous release (`0010_chat_runtime`), and downgrade/upgrade of Phase 6.1/6.2 indexes, point the following destructive check at a **dedicated disposable PostgreSQL database**. It downgrades its schema to `base`:

```powershell
$env:DATABASE_URL = "postgresql+psycopg://movie_agent:movie_agent@localhost:5432/movie_agent_migration_check"
python scripts/verify_migrations.py --yes-destroy-schema
```

The verifier checks the PostgreSQL index catalog as well as all three migration paths. The current hot-query indexes are declared in the baseline model metadata, so the 0011 downgrade removes only indexes that revision 0011 itself created and preserves baseline indexes. Do not run the verifier against a database containing data you need.
