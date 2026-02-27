from app.database import SessionLocal
from app.services.video_factory import VideoFactory
from app.models import Job

def process_job_task(job_id: int):
    """Tarefa executada pelo Worker"""
    db = SessionLocal()
    try:
        factory = VideoFactory(db)
        job = db.query(Job).get(job_id)
        if not job:
            print(f"Job {job_id} not found")
            return
            
        print(f"Worker Processing Job {job_id} - Step: {job.step}")
        factory.process_job(job)
    except Exception as e:
        print(f"Error in job {job_id}: {e}")
    finally:
        db.close()
