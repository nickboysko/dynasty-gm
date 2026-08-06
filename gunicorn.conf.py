"""
Gunicorn config (auto-loaded from the working directory -- no --config flag
needed). post_fork is gunicorn's documented hook for per-worker init that
must run inside the actual worker process, not at module-import time.

Why this exists: gunicorn's master process may import app.py (e.g. to
validate the `app:app` callable) before forking any workers. fork() does not
carry running threads into the child, so anything app.py did at bare
module-import time -- like kicking off the background auto-refresh thread --
could end up running (and completing) entirely inside the master, which
never serves HTTP. The worker that actually handles every request would
inherit a frozen snapshot of STATE/UPDATE_STATUS from fork time and never
see any of that work. Explicitly running startup here, inside post_fork,
guarantees it happens in the worker.
"""


def post_fork(server, worker):
    import app as app_module
    app_module.start_worker()
