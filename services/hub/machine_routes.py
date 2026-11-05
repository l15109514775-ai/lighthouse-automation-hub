"""Version dispatch without converting old evidence into a new permission."""
from .errors import HubError
from .machine_tweets import MachineTweets,VERSION as V1
from .machine_tweets_v2 import MachineTweetsV2,VERSION as V2
from .machine_tweets_api import MachineTweetsAPI,MachineTweetsAPIReference,VERSION as API,REFERENCE_VERSION as API_REFERENCE


def is_machine_policy(value):
    return isinstance(value,str) and any(value==v or value.startswith(v+':') for v in (V1,V2,API,API_REFERENCE))


def machine_service(settings,store,policy=None):
    value=settings.config.get('policy_version') if policy is None else policy
    if value==API_REFERENCE or (isinstance(value,str) and value.startswith(API_REFERENCE+':')):return MachineTweetsAPIReference(settings,store)
    if value==API or (isinstance(value,str) and value.startswith(API+':')):return MachineTweetsAPI(settings,store)
    if value==V2 or (isinstance(value,str) and value.startswith(V2+':')):return MachineTweetsV2(settings,store)
    if value==V1 or (isinstance(value,str) and value.startswith(V1+':')):return MachineTweets(settings,store)
    raise HubError('MACHINE_TWEET_POLICY_UNSUPPORTED')


class MachineRoutes:
    def __init__(self,settings,store):self.settings,self.store=settings,store

    def dispatch(self,job):
        service=machine_service(self.settings,self.store,job['payload'].get('version'))
        if self.settings.config['policy_version']!=service.version:
            rows=self.store.query("SELECT 1 FROM publications WHERE job_id=? AND kind=? AND state IN ('dispatching','unknown','completed')",
                (job['payload'].get('parent_job_id'),service.kind))
            if not rows:raise HubError('MACHINE_SOURCE_GENERATION_NOT_ACTIVE')
        try:return service.dispatch(job)
        except HubError as exc:
            if exc.review:raise HubError(exc.code,retryable=exc.retryable,retry_after=exc.retry_after) from None
            raise

    def reconcile(self,job):
        values=[]
        for cls in (MachineTweets,MachineTweetsV2,MachineTweetsAPI,MachineTweetsAPIReference):
            service=cls(self.settings,self.store)
            if self.store.query("SELECT 1 FROM publications WHERE kind=? AND state IN ('dispatching','unknown')",(service.kind,)):
                values.append(service.reconcile(job))
        return {'versions':values,'publish_requests_sent':0}
