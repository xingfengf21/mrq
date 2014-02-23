import gevent
import gevent.pool
import os
import signal
import datetime
import time
import socket
import traceback
import psutil
import sys
from bson import ObjectId

from .job import Job
from .exceptions import JobTimeoutException, StopRequested, JobInterrupt
from .context import set_current_worker, set_current_job, get_current_job, connections
from .queue import Queue

# https://groups.google.com/forum/#!topic/gevent/EmZw9CVBC2g
# if "__pypy__" in sys.builtin_module_names:
#   def _reuse(self):
#       self._sock._reuse()
#   def _drop(self):
#       self._sock._drop()
#   gevent.socket.socket._reuse = _reuse
#   gevent.socket.socket._drop = _drop


class Worker(object):

  # Allow easy overloading
  job_class = Job

  def __init__(self, config):

    self.config = config

    set_current_worker(self)
    #queues, pool_size=1, max_jobs=None, redis=None, mongodb_jobs=None, mongodb_logs=None, name=None):

    self.datestarted = datetime.datetime.utcnow()
    self.status = "init"
    self.queues = [x for x in self.config["queues"] if x]
    self.redis_queues = [Queue(x).redis_key for x in self.queues]
    self.done_jobs = 0
    self.max_jobs = self.config["max_jobs"]

    self.connected = False  # MongoDB + Redis

    self.process = psutil.Process(os.getpid())

    self.id = ObjectId()
    if config["name"]:
      self.name = self.config["name"]
    else:
      self.name = self.make_name()

    self.pool_size = self.config["pool_size"]

    from .logger import LogHandler
    self.log_handler = LogHandler(quiet=self.config["quiet"])
    self.log = self.log_handler.get_logger(worker=self.id)

    self.log.info("Starting Gevent pool with %s worker greenlets (+ 1 monitoring)" % self.pool_size)

    self.gevent_pool = gevent.pool.Pool(self.pool_size)

    # Keep references to main greenlets
    self.greenlets = {}

    self.profiler = None
    if self.config["profile"]:
      print "Starting profiler..."
      import cProfile
      self.profiler = cProfile.Profile()
      self.profiler.enable()

  def connect(self, force=False):

    if self.connected and not force:
      return

    # Accessing connections attributes will automatically connect
    self.redis = connections.redis
    self.mongodb_jobs = connections.mongodb_jobs
    self.mongodb_logs = connections.mongodb_logs

    self.log_handler.set_collection(self.mongodb_logs.mrq_logs)

    self.connected = True

  def make_name(self):
    """ Generate a human-readable name for this worker. """
    return "%s.%s" % (socket.gethostname().split(".")[0], os.getpid())

  def greenlet_scheduler(self):

    from .scheduler import Scheduler
    scheduler = Scheduler(self.mongodb_jobs.scheduled_jobs)

    scheduler.sync_tasks()

    while True:
      scheduler.check()
      time.sleep(int(self.config["scheduler_interval"]))

  def greenlet_monitoring(self):
    """ This greenlet always runs in background to update current status in MongoDB every 10 seconds.

    Caution: it might get delayed when doing long blocking operations. Should we do this in a thread instead?
     """

    while True:

      # print "Monitoring..."

      self.report_worker()
      self.flush_logs(w=0)
      time.sleep(int(self.config["report_interval"]))

  def report_worker(self, w=0):

      greenlets = []

      for greenlet in self.gevent_pool:
        g = {}
        short_stack = []
        stack = traceback.format_stack(greenlet.gr_frame)
        for s in stack[1:]:
          if "/gevent/hub.py" in s:
            break
          short_stack.append(s)
        g["stack"] = short_stack

        job = get_current_job(id(greenlet))
        if job:
          if job.data:
            g["path"] = job.data["path"]
          g["datestarted"] = job.datestarted
          g["id"] = job.id
        greenlets.append(g)

      cpu = self.process.get_cpu_times()

      # Avoid sharing passwords or sensitive config!
      whitelisted_config = [
        "max_jobs",
        "pool_size",
        "queues",
        "name"
      ]

      self.mongodb_logs.mrq_workers.update({
        "_id": ObjectId(self.id)
      }, {"$set": {
        "status": self.status,
        "config": {k: v for k, v in self.config.iteritems() if k in whitelisted_config},
        "done_jobs": self.done_jobs,
        "datestarted": self.datestarted,
        "datereported": datetime.datetime.utcnow(),
        "name": self.name,
        "id": self.id,
        "process": {
          "pid": self.process.pid,
          "cpu": {
            "user": cpu.user,
            "system": cpu.system,
            "percent": self.process.get_cpu_percent(0)
          },
          "mem": {
            "rss": self.process.get_memory_info().rss
          }
          # https://code.google.com/p/psutil/wiki/Documentation
          # get_open_files
          # get_connections
          # get_num_ctx_switches
          # get_num_fds
          # get_io_counters
          # get_nice
        },
        "jobs": greenlets
      }}, upsert=True, w=w)

  def flush_logs(self, w=0):
    self.log_handler.flush(w=w)

  def dequeue_jobs(self, max_jobs=1):
    """ Fetch a maximum of max_jobs from this worker's queues. """

    self.log.debug("Fetching %s jobs from Redis" % max_jobs)

    jobs = []
    queue, job_id = self.redis.blpop(self.redis_queues, 0)

    # From this point until job.fetch_and_start(), job is only local to this worker.
    # If we die here, job will be lost in redis without having been marked as "started".

    jobs.append(self.job_class(job_id, worker=self, queue=queue, start=True))

    # Bulk-fetch other jobs from that queue to fill the pool.
    # We take the chance that if there was one job on that queue, there should be more.
    if max_jobs > 1:

      with self.redis.pipeline(transaction=False) as pipe:
        for _ in range(max_jobs - 1):
          pipe.lpop(queue)
        job_ids = pipe.execute()

      jobs += [self.job_class(_job_id, worker=self, queue=queue, start=True)
               for _job_id in job_ids if _job_id]

    return jobs

  def work_loop(self):
    """Starts the work loop.

    """

    self.connect()

    self.status = "started"

    self.greenlets["monitoring"] = gevent.spawn(self.greenlet_monitoring)

    if self.config["scheduler"]:
      self.greenlets["scheduler"] = gevent.spawn(self.greenlet_scheduler)

    self.install_signal_handlers()

    try:

      while True:

        while True:
          free_pool_slots = self.gevent_pool.free_count()
          if free_pool_slots > 0:
            break
          gevent.sleep(0.01)

        self.log.info('Listening on %s' % self.queues)

        jobs = self.dequeue_jobs(max_jobs=free_pool_slots)

        for job in jobs:

          self.gevent_pool.spawn(self.perform_job, job)

          self.done_jobs += 1

        if self.max_jobs and self.max_jobs >= self.done_jobs:
          self.log.info("Reached max_jobs=%s" % self.done_jobs)
          break

    except StopRequested:
      pass

    finally:

      try:

        self.log.debug("Joining the greenlet pool...")
        self.status = "stopping"

        self.gevent_pool.join(timeout=None, raise_error=False)
        self.log.debug("Joined.")

      except StopRequested:
        pass

      self.gevent_pool.kill(exception=JobInterrupt, block=True)

      for g in self.greenlets:
        self.greenlets[g].kill(block=True)
        self.log.debug("Greenlet for %s killed." % g)

      self.report_worker(w=1)
      self.flush_logs(w=1)

      if self.profiler:
        self.profiler.print_stats(sort="cumulative")

  def perform_job(self, job):
    """ Wraps a job.perform() call with timeout logic and exception handlers.

        This is the first call happening inside the greenlet.
    """

    set_current_job(job)

    gevent_timeout = gevent.Timeout(job.timeout, JobTimeoutException(
      'Gevent Job exceeded maximum timeout value (%d seconds).' % job.timeout
    ))

    gevent_timeout.start()

    try:
      job.perform()

    except job.retry_on_exceptions:
      job.save_retry(sys.exc_info()[1], traceback=traceback.format_exc())

    except JobTimeoutException:
      trace = traceback.format_exc()
      self.log.error("Job timeouted after %s seconds" % job.timeout)
      self.log.error(trace)
      job.save_status("timeout", traceback=trace)

    except JobInterrupt:
      trace = traceback.format_exc()
      self.log.error(trace)
      job.save_status("interrupt", traceback=trace)

    except Exception:
      trace = traceback.format_exc()
      self.log.error(trace)
      job.save_status("failed", traceback=trace)

    finally:
      gevent_timeout.cancel()
      set_current_job(None)

  def shutdown_graceful(self):
    """ Graceful shutdown: waits for all the jobs to finish. """

    self.log.info("Graceful shutdown...")
    raise StopRequested()

  def shutdown_now(self):
    """ Forced shutdown: interrupts all the jobs. """

    self.log.info("Forced shutdown...")
    self.status = "killing"

    self.gevent_pool.kill(exception=JobInterrupt, block=False)

    raise StopRequested()

  def install_signal_handlers(self):
    """ Handle events like Ctrl-C from the command line. """

    self.graceful_stop = False

    def request_shutdown_now():
      self.shutdown_now()

    def request_shutdown_graceful():

      # Second time CTRL-C, shutdown now
      if self.graceful_stop:
        request_shutdown_now()
      else:
        self.graceful_stop = True
        self.shutdown_graceful()

    # First time CTRL-C, try to shutdown gracefully
    gevent.signal(signal.SIGINT, request_shutdown_graceful)

    # User (or Heroku) requests a stop now, just mark tasks as interrupted.
    gevent.signal(signal.SIGTERM, request_shutdown_now)
