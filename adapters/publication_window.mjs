// Check again immediately before any actual send control, including terms.
export function assertPublicationWindow(spec,now=Date.now()) {
 if(spec.machine_policy===undefined)return;
 if(!['machine-tweet-v1','machine-tweet-v2','machine-tweet-v3-api','machine-tweet-v4-api-relations'].includes(spec.machine_policy)||!Number.isFinite(spec.dispatch_expires_at)||
  spec.dispatch_expires_at<=0)throw new Error('MACHINE_DISPATCH_WINDOW_INVALID');
 if(now>=spec.dispatch_expires_at*1000)throw new Error('OWNER_TWO_HOUR_WINDOW_EXPIRED');
}
