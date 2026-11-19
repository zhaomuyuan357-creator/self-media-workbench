"""Small application service facade for the workbench's main workflows."""


class WorkbenchService:
    """Coordinates repository work without depending on HTTP request objects."""

    def __init__(self, repository, publish_pool, publish_worker):
        self.repository = repository
        self.publish_pool = publish_pool
        self.publish_worker = publish_worker

    def enqueue_publish(self, job, request, idempotency_key, publisher):
        return self.publish_pool.submit(
            self.publish_worker, job["id"], request, idempotency_key, publisher,
        )

    def verify_publish_job(self, job_id, outcome, note=""):
        return self.repository.verify_job(job_id, outcome, note)
