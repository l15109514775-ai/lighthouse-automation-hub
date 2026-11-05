"""Uncredentialed HTTPS reachability only. HTTP reachability is not API authorization."""
import json
import os
import urllib.error
import urllib.request
from urllib.parse import urlsplit

targets = {'x_public':'https://x.com/','x_api':'https://api.x.com/2/users/by/username/example_handle_a',
           'youtube':'https://www.youtube.com/@example-channel-a','youtube_api':'https://www.googleapis.com/youtube/v3/channels',
           'github':'https://github.com/','bilibili':'https://www.bilibili.com/','bilibili_open':'https://open.bilibili.com/'}
if os.getenv('MODEL_BASE_URL'):
    parsed = urlsplit(os.environ['MODEL_BASE_URL'])
    targets['selected_model'] = f'{parsed.scheme}://{parsed.netloc}/'
report = {}
for name,url in targets.items():
    try:
        request = urllib.request.Request(url, method='HEAD', headers={'User-Agent':'LighthouseHub-NetworkProbe/0.1'})
        with urllib.request.urlopen(request, timeout=12) as response:
            report[name] = {'tls_reachable':True,'http_status':response.status,'authenticated':False}
    except urllib.error.HTTPError as error:
        report[name] = {'tls_reachable':True,'http_status':error.code,'authenticated':False}
    except Exception:
        report[name] = {'tls_reachable':False,'error_code':'NETWORK_OR_TLS_FAILURE','authenticated':False}
if 'selected_model' not in report:
    report['selected_model'] = {'status':'not_selected_no_paid_call'}
print(json.dumps(report, indent=2))
