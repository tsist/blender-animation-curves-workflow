"""Error handling shared by both UI owners; no worker or scene operations."""
def held(job):
 return job.slot_token is not None or (job.process is not None and not job.waited)
def ensure_timer(job,timers,tick):
 if held(job) or job.state=='CLEANUP_PENDING':
  if not timers.is_registered(tick):timers.register(tick,first_interval=.25)
def failure(job,timers,tick,error):
 messages=[str(error)]
 # Install continuation before a possibly failing close.
 try:ensure_timer(job,timers,tick)
 except BaseException as exc:messages.append('Cleanup timer blocked: '+repr(exc))
 try:
  if held(job) or job.state=='CLEANUP_PENDING':job.close()
 except BaseException as exc:messages.append('Cleanup pending; owner retained: '+repr(exc))
 try:ensure_timer(job,timers,tick)
 except BaseException as exc:messages.append('Automatic cleanup blocked; retry cancel or unload after timer recovery: '+repr(exc))
 return '; '.join(messages)
def close_for_unload(job,timers,tick):
 try:job.close()
 except BaseException as exc:
  try:ensure_timer(job,timers,tick)
  except BaseException:pass
  raise RuntimeError('Unregister blocked: cleanup owner/registration retained') from exc
 if held(job):raise RuntimeError('Unregister blocked: live owner retained')
