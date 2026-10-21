import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, ExportJob, User
from app.services import export_service


@pytest.fixture
def export_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'export.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(export_service, "SessionLocal", factory)
    with factory() as db:
        user = User(username="export-user", email="export@example.test", password_hash="x")
        db.add(user)
        db.commit()
        db.refresh(user)
        job = ExportJob(user_id=user.id)
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    yield factory, job_id
    engine.dispose()


def test_update_progress_persists_stage_without_requiring_a_callers_db_session(
    export_db,
):
    factory, job_id = export_db

    export_service._update_progress(job_id, 55, "collections")

    with factory() as db:
        job = db.get(ExportJob, job_id)
        assert job.progress == 55
        assert job.stage == "collections"
