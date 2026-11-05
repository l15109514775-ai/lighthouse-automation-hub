from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import math
import time
import uuid
import json
from .errors import HubError

X_PRICING_VERSION = 'x-read-resources-2026-11-01-v1'
X_POST_USD = .005
X_USER_USD = .010
X_PRICING_SOURCE = {
    'basis': 'X public read-resource pricing table confirmed by operator on 2026-11-01',
    'source': 'https://docs.x.com/x-api/getting-started/pricing',
    'post_usd': X_POST_USD, 'user_usd': X_USER_USD,
    'scope': 'Read Posts and Users. attachments.media_keys is post data; the supplied read table has no separately priced Media item. No media-write endpoint is used.',
    'estimate_only': True, 'invoice_charge_verified': False,
    'repeated_returns': 'Count every returned resource again; no 24-hour deduplication credit assumed.',
}


class Budget:
    def __init__(self, store, limits):
        self.store, self.limits = store, limits

    def reserve(self, provider, job_id=None, *, units=1, reserved_cost_usd=None, x_resource_bounds=None):
        limit = self.limits[provider]
        utc_estimate = limit.get('utc_resource_dedup_estimate_enabled',False) if provider=='x' else False
        if type(utc_estimate) is not bool:
            raise HubError('X_COST_ACCOUNTING_MODE_INVALID',review=True)
        unlimited = limit.get('unlimited', False)
        if unlimited and (provider != 'model' or not limit['paid_authorized']):
            raise HubError('UNLIMITED_BUDGET_NOT_AUTHORIZED', review=True)
        try:
            cost = float(limit['max_call_usd'])
        except (TypeError, ValueError, OverflowError):
            raise HubError('INVALID_BUDGET', review=True) from None
        if provider == 'x' and (type(units) is not int or units != 1):
            raise HubError('X_HTTP_ATTEMPT_UNIT_REQUIRED', review=True)
        if not math.isfinite(cost) or cost < 0 or units < 1:
            raise HubError('INVALID_BUDGET', review=True)
        if provider == 'x':
            # X bills returned resources, not one resource per HTTP request.
            # The adapter supplies a worst-case bound for this endpoint and its
            # expansions before any network activity. The configured per-call
            # amount remains a ceiling, not a flat estimated X response price.
            if cost <= 0:
                raise HubError('BILLABLE_COST_BOUND_REQUIRED', review=True)
            if reserved_cost_usd is None:
                raise HubError('X_RESOURCE_COST_BOUND_REQUIRED', review=True)
            if (isinstance(reserved_cost_usd, bool) or
                    not isinstance(reserved_cost_usd, (int, float)) or
                    not math.isfinite(reserved_cost_usd) or reserved_cost_usd <= 0):
                raise HubError('X_RESOURCE_COST_BOUND_INVALID', review=True)
            if (not isinstance(x_resource_bounds, dict) or
                    set(x_resource_bounds) != {'endpoint_kind', 'posts', 'users'} or
                    x_resource_bounds['endpoint_kind'] not in ('user_lookup', 'timeline', 'post_detail') or
                    any(type(x_resource_bounds[k]) is not int or not 0 <= x_resource_bounds[k] <= 100
                        for k in ('posts', 'users'))):
                raise HubError('X_RESOURCE_BOUND_REQUIRED', review=True)
            required = x_resource_bounds['posts'] * X_POST_USD + x_resource_bounds['users'] * X_USER_USD
            if required <= 0 or reserved_cost_usd + 1e-9 < required:
                raise HubError('X_RESOURCE_COST_BOUND_TOO_LOW', review=True)
            if reserved_cost_usd > cost + 1e-9:
                raise HubError('API_CALL_BUDGET_EXCEEDED', review=True)
            for period in ('daily', 'monthly'):
                amount, calls = limit[f'{period}_usd'], limit[f'{period}_calls']
                if (isinstance(amount, bool) or not isinstance(amount, (int, float)) or
                        not math.isfinite(amount) or amount < 0 or isinstance(calls, bool) or
                        not isinstance(calls, (int, float)) or not math.isfinite(calls) or
                        calls < 0 or calls != int(calls)):
                    raise HubError('INVALID_BUDGET', review=True)
            cost = float(reserved_cost_usd)
        elif reserved_cost_usd is not None or x_resource_bounds is not None:
            # Preserve the model's independently configured bounded/unlimited
            # semantics; an X-specific override cannot cheapen other providers.
            raise HubError('RESOURCE_COST_OVERRIDE_NOT_SUPPORTED', review=True)
        if cost and not limit['paid_authorized']:
            raise HubError('PAID_SERVICE_NOT_AUTHORIZED', review=True)
        if provider in ('x', 'model') and cost <= 0 and not unlimited:
            raise HubError('BILLABLE_COST_BOUND_REQUIRED', review=True)
        now = datetime.now(ZoneInfo('Asia/Shanghai'))
        daily = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        monthly = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
        ident = str(uuid.uuid4())
        with self.store.transaction() as db:
            for label, start in (('daily', daily), ('monthly', monthly)):
                row = db.execute('SELECT COALESCE(SUM(cost_usd),0) cost,COALESCE(SUM(units),0) calls FROM api_calls WHERE provider=? AND started_at>=?',
                                 (provider, start)).fetchone()
                admitted_cost=row['cost']
                if utc_estimate:
                    from .x_cost_ledger import period_cost
                    admitted_cost=period_cost(db,start)
                # Every in-flight/unknown call still reserves its FULL endpoint
                # bound. Only durable successful receipts earn later credits.
                if not unlimited and (admitted_cost + cost > float(limit[f'{label}_usd']) + 1e-9 or row['calls'] + units > limit[f'{label}_calls']):
                    raise HubError('API_BUDGET_EXCEEDED', review=True)
            db.execute('INSERT INTO api_calls(call_id,provider,job_id,started_at,cost_usd,state,units) VALUES(?,?,?,?,?,?,?)',
                       (ident, provider, job_id, time.time(), cost, 'reserved', units))
            if provider == 'x':
                db.execute('''INSERT INTO api_resource_usage(call_id,pricing_version,pricing_source,endpoint_kind,
                    max_posts,max_users,post_unit_usd,user_unit_usd,reserved_upper_bound_usd,state,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,'reserved',?)''',
                    (ident, X_PRICING_VERSION, json.dumps(X_PRICING_SOURCE, sort_keys=True),
                     x_resource_bounds['endpoint_kind'], x_resource_bounds['posts'], x_resource_bounds['users'],
                     X_POST_USD, X_USER_USD, cost, time.time()))
                if utc_estimate:
                    from .x_cost_ledger import record_reservation
                    record_reservation(db,ident,limit,cost)
        return ident

    def note_x_receipt(self,call_id):
        from .x_cost_ledger import note_receipt
        note_receipt(self.store,call_id)

    def finish(self, call_id, state, code=None, *, resource_counts=None):
        # Only a validated successful X read may lower its pre-request maximum.
        # Failure/timeout/interruption reservations retain the full upper bound.
        with self.store.transaction() as db:
            resource = db.execute('SELECT * FROM api_resource_usage WHERE call_id=?', (call_id,)).fetchone()
            if resource:
                if state == 'done':
                    if resource['state'] != 'reserved':
                        raise HubError('X_RESOURCE_SETTLEMENT_ALREADY_FINALIZED', review=True)
                    if (not isinstance(resource_counts, dict) or set(resource_counts) != {'posts', 'users'} or
                            any(type(resource_counts[k]) is not int or resource_counts[k] < 0 for k in ('posts', 'users'))):
                        raise HubError('X_RESOURCE_USAGE_CONFIRMATION_REQUIRED', review=True)
                    if resource_counts['posts'] > resource['max_posts'] or resource_counts['users'] > resource['max_users']:
                        raise HubError('X_RESOURCE_RESPONSE_BOUND_EXCEEDED', review=True)
                    estimate = resource_counts['posts'] * resource['post_unit_usd'] + resource_counts['users'] * resource['user_unit_usd']
                    if estimate > resource['reserved_upper_bound_usd'] + 1e-9:
                        raise HubError('X_RESOURCE_RESPONSE_BOUND_EXCEEDED', review=True)
                    db.execute('UPDATE api_calls SET cost_usd=? WHERE call_id=?', (estimate, call_id))
                    db.execute('''UPDATE api_resource_usage SET returned_posts=?,returned_users=?,
                        settled_upper_bound_usd=?,state='estimated',settled_at=? WHERE call_id=?''',
                        (resource_counts['posts'], resource_counts['users'], estimate, time.time(), call_id))
                else:
                    db.execute('UPDATE api_calls SET cost_usd=? WHERE call_id=?',
                        (resource['reserved_upper_bound_usd'], call_id))
                    db.execute('''UPDATE api_resource_usage SET state=?,settled_upper_bound_usd=NULL,settled_at=? WHERE call_id=?''',
                        ('unknown_upper_bound' if state == 'unknown' else 'error_upper_bound', time.time(), call_id))
            db.execute('UPDATE api_calls SET state=?,error_code=?,finished_at=? WHERE call_id=?',
                       (state, code, time.time(), call_id))

    def report(self):
        now = datetime.now(ZoneInfo('Asia/Shanghai'))
        periods = {'daily': now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp(),
                   'monthly': now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()}
        report = {name: self.store.query('''SELECT a.provider,COUNT(*) attempts,SUM(a.units) quota_units,
                   SUM(a.cost_usd) reserved_upper_bound_usd, SUM(u.input_tokens) input_tokens,SUM(u.output_tokens) output_tokens,
                   SUM(u.estimated_cost_usd) estimated_cost_usd,
                   SUM(CASE WHEN a.provider='model' AND u.estimated_cost_usd IS NULL THEN 1 ELSE 0 END) unknown_cost_calls
                   FROM api_calls a LEFT JOIN api_usage_details u ON u.call_id=a.call_id
                   WHERE a.started_at>=? GROUP BY a.provider''', (start,)) for name,start in periods.items()}
        report['model_budget_unlimited'] = bool(self.limits['model'].get('unlimited',False))
        report['cost_note'] = 'Unknown estimates are not zero charges. Relay invoice pricing must be configured separately.'
        report['x_cost_note'] = ('X quota_units counts HTTP attempts, not returned resources. '
            'Validated successful reads settle to returned Post/User resource estimates; errors/unknown retain the full pre-request maximum. '
            'The original gross ledger assumes no daily-deduplication credit. The separate UTC resource estimate is a soft-guarantee estimate, '
            'not an invoice or confirmed charged amount.')
        report['x_resource_usage'] = self.store.query('''SELECT pricing_version,state,COUNT(*) attempts,
            SUM(max_posts) reserved_post_bounds,SUM(max_users) reserved_user_bounds,
            SUM(returned_posts) returned_posts,SUM(returned_users) returned_users,
            SUM(reserved_upper_bound_usd) pre_request_upper_bound_usd,
            SUM(settled_upper_bound_usd) confirmed_response_estimate_usd
            FROM api_resource_usage GROUP BY pricing_version,state''')
        from .x_cost_ledger import report as x_cost_report
        report['x_utc_resource_estimate']=x_cost_report(self.store,periods)
        mode=self.limits['x'].get('utc_resource_dedup_estimate_enabled',False)
        report['x_budget_basis']=('rejected_invalid_accounting_mode' if type(mode) is not bool else
            'gross_conservative_upper_bound' if not mode else 'utc_resource_estimate_plus_unverified_gross_v1'
            if report['x_utc_resource_estimate'].get('calibration_ready') else 'blocked_uncalibrated_utc_resource_estimate')
        return report
