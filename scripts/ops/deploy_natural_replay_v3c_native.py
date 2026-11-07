"""V3c native delta: genuine pilot admission and exact Natural345 qualification.

Never admits a job from the host, resets data, buys resources or alters other
services. Candidate authorization bytes must come from the real pilot issuer.
"""
from copy import deepcopy
from datetime import datetime,timezone
from pathlib import Path
import argparse,hashlib,json,os,re,stat,subprocess,tempfile,time,tomllib,types,uuid
import urllib.request
import xml.etree.ElementTree as ET

IMAGE='sha256:9f19b756ca081bfe033ffa01b0face5c9cb70b3d45de08394567150ac747de6e'
HELPER_SHA='50144dbe19d38329b549ca7fb5e64e2ea9454a07d4b48fd5ca02bd6efcf35cb1'
CACHE_OPERATOR_SHA='3b6093639434dae80cadbe65392f444f7f45b3b4455d1ea53fd2ab4688e991b8'
CURRENT_OPERATOR_COMMIT='140d728-navigation-over-A68'
# These are immutable candidate identities, not a whole-tree release claim.
PARENT_INSTALLER_SHA='6bf479fe25313ee2845702dafe59c51f2d7e6241fd0fb245d168f52a92f81e6f'
KNOWN_ACTIVATION_BLOCKERS=('NATURAL_PRODUCER_HISTORY_WINDOW_STARVATION',)
TARGET_ARCHIVE='d8b2193d0d5e229f322a440f73c549a468ceac8af4c25f54eb834d7a6a0f854b'
TARGET_MANIFEST_SHA='a477d23b70603bf1dca937d6653dc866bcdcd16184ce7ded3f26789029784390'
TARGET_ATTESTATION_SHA='0a2d704695f6fde158e73a3524859838731b9cdf00121712babd4e4c26dc6f75'
TARGET_PARENT='8e41e5e88e88827e300963a57f4ab4f6564d4f1ae75a8693c1a62c302644efa9'
NAV='adapters/short_video_saved_draft_navigation_control.mjs'
NAV_SHA='27770670eb8fddc997f4e6b2c2c8f6f9a17804d42b41097fca45f04e8e090a69'
NAV_OLD='4a97c6c9833a9c58705492ccdbd12db2ac00cbbd564f100e8324085e8d09ef0e'
NAV_COMMIT='140d72899cc5a79d177c7ba69891b8d561b199db'
PY_COMMIT='c8986648255244274bbfd1d2a882d5e06cf5681d'
UI_COMMIT='3b48ec2cc092a13273ca6f7a2809319478d42d49'
GLOBAL_HUB_SHA='e2b874d7ffb40600083ec534fc8ae449b4dd6f42ff9b775dacbe1d6e89b43df8'
SCHEDULER_SHA='2afcaf01cf53f702768ccf6d90517a4730b25b8bec33ea261d66662f8c4143c4'
CURRENT_SPECS={
 'repair':('native-snapshot-metadata-v2mix68-ubuntu-tests-v1','native-snapshot-metadata-v2mix68-ro-overlay-v1',
  'native-snapshot-metadata-v2mix68-v1-ro-overlay-','lighthouse-native-snapshot-metadata-v2mix68-v1-owned-',68,92,
  'e092f9d0ce11e83feb8abf3444e90c822be6c64210a6cbeb14329d2fbdfb1ec9'),
 'navigation':('saved-draft-manager-wait-v3-five-ubuntu-tests-v1','saved-draft-manager-wait-v3-ro-overlay-v1',
  'saved-draft-manager-wait-v3-five-ro-overlay-','lighthouse-saved-draft-manager-wait-v3-five-owned-',5,94,
  '88d9f5e39ad4d655e0943b49882ec9e4771e31354c7e4221f9bbb4805d97c110')}
CURRENT_ATTESTATIONS={'repair':'d1cd38f4141eb8a238fa9b126ff208de7ea26a98aa9b0da691ed549a817aa92a',
 'navigation':'743a27c47320c5cd8ba95932a0e8a55bec2babfac1b843c89776a651fa788fb3'}
TARGET_RUNTIME_PINS={'adapters/bilibili_video_guard.mjs': '1ecb678df412dd4aeb2680b4762e82b04bf7083484da81ee8df90815fc43069c',
 'adapters/browser_replay_video.mjs': '61a249b918af9371858f677dc2629ab9d3898453b02c930b3aa9236004aa7f1a',
 'adapters/media_tools.py': '434c5c098a6dd3520b88f57823408d50dc46108438c22e081fed9be90876d4e8',
 'adapters/recovered_submit_gate.mjs': '6933998117e6b1c3e8c5ff455ca7a616233831fae62565ee9512a8f3d4cc277a',
 'adapters/replay_highest_contract_v2.mjs': '3f2bd06a3dbb379ad4db675f61b37de2162f9e232805cc23950be504e936feff',
 'adapters/replay_native_driver.mjs': '4c8dbdea7d04ab2192552f7b9cc1ad370d6cec2d6ba76121342d600af3eafddb',
 'adapters/replay_natural_contract_v3.mjs': '0888e2e9b4a089c833237984e12179f06129dfaa3126e607f93db96f46bf2adc',
 'adapters/safe_https.py': '8d393985e41f2409e9a1fa738e3ed2d2842d7dc4bd02ebe9c79a25bb9b01149f',
 'adapters/short_video_metadata_chain.mjs': '0a096995b314afe88e07f9afd082d1072704cfb519a1d33283067417d4ebae89',
 'adapters/short_video_saved_draft_navigation_control.mjs': '27770670eb8fddc997f4e6b2c2c8f6f9a17804d42b41097fca45f04e8e090a69',
 'adapters/youtube.py': '82a1d645a342665188e6cbe53cc2a821b3ff57492e3360f3427ba403dfae1fb0',
 'adapters/youtube_feed.py': '718d398f92eb425c2ae4165a9756b05840820325f0f4d665b06398e5f26ed5aa',
 'adapters/youtube_readiness_evidence_v3.py': '1899af9bac102152dda8a724f1da22ddaf715c82fff32726d82a1752b31fa9a9',
 'adapters/ytdlp_highest_v2.py': '4bd41c848335e9a93bcccd985a159f33dfe6d2ef284e2dda43df2c46a4cadff8',
 'adapters/ytdlp_natural_plan_v3.py': '7fa9553b2f4d24d9d6412937b49980c48443e60019757f20d2b4fbe4891f193d',
 'adapters/ytdlp_natural_readiness_v3.py': 'ccb6a09122f87450704da5bf505f3b83525a8df9b7463c7352924e959f38e1fa',
 'adapters/ytdlp_natural_v3.py': '7ce79506d7e2fa52d63935c80a7c4a0a87f4f92f4fd79908ac6bd8ad0cecf1a0',
 'adapters/ytdlp_replay.py': '2d0e5867b870ba28d5595128488e0d895dcd042a38aa110266e42dfb302b8b8f',
 'scheduler/service.py': '53412a81121d98790e4a7a1d425de7517135afea8cb4bf5d83144288a4e9cd97',
 'scripts/ops/native_control_sources/short-video-native-metadata-v5.mjs': 'd25de4d718bff78d487fb3aae7e9b1739a4ddfbef2d5eb170843049d283d5e23',
 'services/hub/budget.py': '1de68d1a9937e229be76fc5fe2de21ae1814a9cfc0158096f86bc67bd87f827e',
 'services/hub/config.py': 'f3d039c30fce7aa0051f597b1830fed5170bf715e0af6151eaef774b83ed8ec2',
 'services/hub/cover_normalizer_v4.py': 'bbc133b7469578650069d6ecc32b593c6b141a55e56afcdb52938eb2558b00ae',
 'services/hub/db.py': 'a5c8134ba961621793e7f4ad536d785d936e8f53ff439add448d7b0223c48e3d',
 'services/hub/errors.py': 'e33f726574126cf7dfbbafbdaeb514aa8d92addc66801f6282b5c01a38c8039b',
 'services/hub/evidence.py': '1ede4fb0b89951394f50f3e31e2e80e579196dac7b9fac9284cd0fafdce567a2',
 'services/hub/locks.py': 'ca7433c198f96cfdfa0c5aaea99dc789354a19d60e0f5ea59b21a6b4063dace9',
 'services/hub/maintenance.py': '7f78219d4a0e097b95791e655e31b766befb6d3ce393b8c90b10874784c03f03',
 'services/hub/media_processing.py': 'cdd769af85dddfbdf7b736f38280e2aa90d5dcd4302d3cf09a16a9b46ca63869',
 'services/hub/outcomes.py': '3a55475a66626538afa0e6215964bff7e6cafedf367a934173ced5a7893c16a1',
 'services/hub/recovered_submit_ticket.py': 'cdb31fbd7542c6a61272751c446d5650406d75f329e9a48215f9a2259a096e12',
 'services/hub/replay_highest_coverage_v2.py': 'dbba8457a11c01f46d64364993f3a5c19ddfe0a81149b8b1dfc980f96f31602e',
 'services/hub/replay_highest_limits_v2.py': 'e6bc3b1ac860797b4f786a69ea39b7443302f2a5bcad0541f70e8faf95f688b5',
 'services/hub/replay_highest_video_v2.py': '2481d6a2122b270f7bd92d4d9c639666d973ac62f1a49848ba4621398e28aac0',
 'services/hub/replay_highest_worker_runtime_v2.py': 'e43b25f10145e755f62671c22f70b2d70c8e73f3060f66c60105c382b5980235',
 'services/hub/replay_machine_checks.py': '1879757741d61173c9b5a27e6e3a210294b4c3efa9952e6b9483e97dd1fb6588',
 'services/hub/replay_moderation_common.py': 'beb29443f24316c37859d4fdba94f4a0a9ad505a327af58323ac30f26728c9e1',
 'services/hub/replay_natural_launch_v3.py': 'cfc3b5a730ff0a9a38684cbd0c3a5383d238273bf011ffd1caacb457245cdb48',
 'services/hub/replay_natural_moderation_v3.py': 'bb9d5492ad912aa2b74e49a405476ce7c9040f276ba00ab2b635853c34197beb',
 'services/hub/replay_natural_video_v3.py': '572f59f809ea37bbed9e3fb893f1252182e22ec98c645ecc67fd3d3765f19c7c',
 'services/hub/replay_natural_worker_runtime_v3.py': '73685a6f0f5b9a3b44de79e5d8dfd89972c86c40a4cd9a053f8430fcd044b0d0',
 'services/hub/replay_packet_cache_v1.py': '7727417e5064cd669d416ce9cee4209caaddc8b3e84a59c490031ad5c6b2a2f1',
 'services/hub/replay_video_browser.py': '2b0fcdf25c67ccf339d380b972a62d514bb82726bcd2ebb7a58c598b7c13573f',
 'services/hub/replay_video_cover.py': '51f4367d9fc7a94cb88d95a169f9dce1e105f585f0d4c18a79964880494e3527',
 'services/hub/replay_video_web.py': 'd82ef20a0e990d835577b0737cf2eefd2324b4215026352963694ee18f0e1046',
 'services/hub/replay_worker_runtime.py': 'e593e5d9024007afa36adb9577f9dfe5d31ac8a627f85046c9413d4e716e70c2',
 'services/hub/resource_wait_v1.py': '0527e716782a5217436822d4157f282b2ac235c49d7a070a9fe088b2cde2fb38',
 'services/hub/short_video_web.py': 'ca7f6c3530a9495ce276db4bfa3d11d50cad062a55d4d4e2c33973f73437db3b',
 'services/hub/worker.py': '7775aef7a559995724c9a6dfd1d5885d32b50499343db11ae34afe38bcfba310',
 'services/hub/youtube_download.py': 'd14f7d8c522481a6070f86afd46f402b09b6277212821986e89eba3bf0f0337a',
 'services/hub/youtube_download_v2.py': '76f14796fb3d9b9641099ef2cc59c38f207989ede8acd7e88d678e43315b52d6',
 'services/hub/youtube_download_v3.py': 'fbabef820c284ed832671fc7b0bcab874b033c7bbae97ff7b62f8362fad87132',
 'services/hub/youtube_highest_plan_v2.py': 'cb4cbeda699dd282cd196291c8cf2a16799ab9b2b00c4c268f7745a35f2e6091',
 'services/hub/youtube_monitor.py': '5466acf7505c2808ea00e478836b76ad79ebf5d0693350f2da5be5b0739dc75b',
 'services/hub/youtube_natural_plan_v3.py': 'c3bb5877d24b5788ede9a4ce95ceb88dc4de4a0b6b43396208bac2ab29074a66',
 'services/hub/youtube_readiness_v3.py': '1e2173ee546a9f59a664148de1dbdab79f8f19e3dcf354bd315f420a09a31c93',
 'vendor/subtitle-cloud-worker/observer/store.py': '5f89202fa37c09200236a940ea6f8ad396d389a19364ab8034a736bd53430673'}
CURRENT_RUNTIME_PINS={'adapters/bilibili_video_guard.mjs': '1ecb678df412dd4aeb2680b4762e82b04bf7083484da81ee8df90815fc43069c',
 'adapters/browser_replay_video.mjs': '6cf718ee0be6e74155edc06cfd3c4f60d35b6d3c00983029051fb99151e401de',
 'adapters/media_tools.py': '434c5c098a6dd3520b88f57823408d50dc46108438c22e081fed9be90876d4e8',
 'adapters/recovered_submit_gate.mjs': '6933998117e6b1c3e8c5ff455ca7a616233831fae62565ee9512a8f3d4cc277a',
 'adapters/replay_highest_contract_v2.mjs': 'd432409b2013889981ad61fc2b76ff3a8795c9bbcd3dc2d9a6afa9eb4bc2de0b',
 'adapters/replay_native_driver.mjs': '3bd436d891e30abc4c4a4fb9dacf80030f1d6356871eb77b0aa1bcf577a1a175',
 'adapters/safe_https.py': '8d393985e41f2409e9a1fa738e3ed2d2842d7dc4bd02ebe9c79a25bb9b01149f',
 'adapters/short_video_metadata_chain.mjs': '0a096995b314afe88e07f9afd082d1072704cfb519a1d33283067417d4ebae89',
 'adapters/short_video_saved_draft_navigation_control.mjs': '27770670eb8fddc997f4e6b2c2c8f6f9a17804d42b41097fca45f04e8e090a69',
 'adapters/youtube.py': '82a1d645a342665188e6cbe53cc2a821b3ff57492e3360f3427ba403dfae1fb0',
 'adapters/youtube_feed.py': '718d398f92eb425c2ae4165a9756b05840820325f0f4d665b06398e5f26ed5aa',
 'adapters/ytdlp_highest_v2.py': 'cc652eb1b3b7e1cccd9ee6f6a48039b32f02a16dfc8141397452639d5f37b2ca',
 'adapters/ytdlp_replay.py': '2d0e5867b870ba28d5595128488e0d895dcd042a38aa110266e42dfb302b8b8f',
 'scheduler/service.py': '53412a81121d98790e4a7a1d425de7517135afea8cb4bf5d83144288a4e9cd97',
 'scripts/ops/native_control_sources/short-video-native-metadata-v5.mjs': 'd25de4d718bff78d487fb3aae7e9b1739a4ddfbef2d5eb170843049d283d5e23',
 'services/hub/budget.py': '1de68d1a9937e229be76fc5fe2de21ae1814a9cfc0158096f86bc67bd87f827e',
 'services/hub/config.py': 'b2e5659c523829a23b64934ce46a679ccc98f4e4fc02670e60f41288dacd399b',
 'services/hub/cover_normalizer_v4.py': 'bbc133b7469578650069d6ecc32b593c6b141a55e56afcdb52938eb2558b00ae',
 'services/hub/db.py': 'a5c8134ba961621793e7f4ad536d785d936e8f53ff439add448d7b0223c48e3d',
 'services/hub/errors.py': 'f15856b1a33b29bc0d40e9297705cea759062dd33ff3cf96f99fd07f6a3e362f',
 'services/hub/evidence.py': '1ede4fb0b89951394f50f3e31e2e80e579196dac7b9fac9284cd0fafdce567a2',
 'services/hub/locks.py': 'ca7433c198f96cfdfa0c5aaea99dc789354a19d60e0f5ea59b21a6b4063dace9',
 'services/hub/maintenance.py': '7f78219d4a0e097b95791e655e31b766befb6d3ce393b8c90b10874784c03f03',
 'services/hub/media_processing.py': 'cdd769af85dddfbdf7b736f38280e2aa90d5dcd4302d3cf09a16a9b46ca63869',
 'services/hub/outcomes.py': '3a55475a66626538afa0e6215964bff7e6cafedf367a934173ced5a7893c16a1',
 'services/hub/recovered_submit_ticket.py': 'cdb31fbd7542c6a61272751c446d5650406d75f329e9a48215f9a2259a096e12',
 'services/hub/replay_highest_coverage_v2.py': '2cbd331c2dcc97985681a8da20d5b31e583de60810ace02a7b7def96432263a3',
 'services/hub/replay_highest_limits_v2.py': 'e6bc3b1ac860797b4f786a69ea39b7443302f2a5bcad0541f70e8faf95f688b5',
 'services/hub/replay_highest_moderation_v2b.py': 'f285c85f2d576d221f5b760902c661e8942c51fd6eb4f7e33a7e416baa5529cd',
 'services/hub/replay_highest_video_v2.py': 'ef6d6abfda2426b6b068682ff0826f7431e464a710f50e6cff6c840330079fee',
 'services/hub/replay_highest_worker_runtime_v2.py': '25ce24bdf003ff8599f350a2cda4d703ce8458a241f95e2fd47816e660ff5b2b',
 'services/hub/replay_machine_checks.py': '1879757741d61173c9b5a27e6e3a210294b4c3efa9952e6b9483e97dd1fb6588',
 'services/hub/replay_moderation_common.py': 'beb29443f24316c37859d4fdba94f4a0a9ad505a327af58323ac30f26728c9e1',
 'services/hub/replay_packet_cache_v1.py': '7727417e5064cd669d416ce9cee4209caaddc8b3e84a59c490031ad5c6b2a2f1',
 'services/hub/replay_video_browser.py': '2b0fcdf25c67ccf339d380b972a62d514bb82726bcd2ebb7a58c598b7c13573f',
 'services/hub/replay_video_cover.py': '51f4367d9fc7a94cb88d95a169f9dce1e105f585f0d4c18a79964880494e3527',
 'services/hub/replay_video_web.py': 'd82ef20a0e990d835577b0737cf2eefd2324b4215026352963694ee18f0e1046',
 'services/hub/replay_worker_runtime.py': 'e593e5d9024007afa36adb9577f9dfe5d31ac8a627f85046c9413d4e716e70c2',
 'services/hub/short_video_web.py': 'ca7f6c3530a9495ce276db4bfa3d11d50cad062a55d4d4e2c33973f73437db3b',
 'services/hub/worker.py': '5e179ae1fb43474487a9e82da3ef342d356ca4702d0e65aca13cf5000a4c6fa7',
 'services/hub/youtube_download.py': 'd14f7d8c522481a6070f86afd46f402b09b6277212821986e89eba3bf0f0337a',
 'services/hub/youtube_download_v2.py': '76f14796fb3d9b9641099ef2cc59c38f207989ede8acd7e88d678e43315b52d6',
 'services/hub/youtube_highest_plan_v2.py': 'cb4cbeda699dd282cd196291c8cf2a16799ab9b2b00c4c268f7745a35f2e6091',
 'services/hub/youtube_monitor.py': '22e1424835378dda85317452c7e23f3fcbbeb51a2efc9970fa998d9f95a60ac7',
 'vendor/subtitle-cloud-worker/observer/store.py': '5f89202fa37c09200236a940ea6f8ad396d389a19364ab8034a736bd53430673'}
TARGET_FROM_295={'adapters/short_video_saved_draft_navigation_control.mjs': '27770670eb8fddc997f4e6b2c2c8f6f9a17804d42b41097fca45f04e8e090a69',
 'scripts/ops/native_control_sources/short-video-native-metadata-v5.mjs': 'd25de4d718bff78d487fb3aae7e9b1739a4ddfbef2d5eb170843049d283d5e23',
 'services/hub/replay_video_browser.py': '2b0fcdf25c67ccf339d380b972a62d514bb82726bcd2ebb7a58c598b7c13573f',
 'services/hub/replay_video_web.py': 'd82ef20a0e990d835577b0737cf2eefd2324b4215026352963694ee18f0e1046',
 'services/hub/short_video_web.py': 'ca7f6c3530a9495ce276db4bfa3d11d50cad062a55d4d4e2c33973f73437db3b',
 'tests/metadata_v5_async_fixture.mjs': '2a303c589a57547ba311d2180f14e167f46a61646b9e930557ad81064b22b6e4',
 'tests/saved_draft_navigation_wait_fixture.mjs': '393e02536e64d819206c2b363b5d0fabe1c880e9f2e18b21f7ed76e6a5ba9691',
 'tests/test_metadata_v5_async.py': 'e5651385bfad709eaec4e4ba81f614afa904224f07fcb886a6035ff833ac4d9c',
 'tests/test_replay_native_snapshot_delta.py': '98481638ddbb5361f91e552088b5c16760bbf52fec7f6683d4f089f11fc399d3',
 'tests/test_saved_draft_navigation_wait.py': 'dfc1d85e14fa57c91e7ad71f9e490ab3a99fce7dc4f90775d3c28b31bea419c8'}
TARGET_GROUPS={'tests/test_metadata_v5_async.py': 11,
 'tests/test_replay_highest_video_v2.py': 28,
 'tests/test_replay_highest_worker_runtime_v2.py': 8,
 'tests/test_replay_machine_checks.py': 22,
 'tests/test_replay_native_driver.py': 11,
 'tests/test_replay_native_snapshot_delta.py': 34,
 'tests/test_replay_natural_launch_v3.py': 3,
 'tests/test_replay_natural_moderation_v3.py': 10,
 'tests/test_replay_natural_video_v3.py': 17,
 'tests/test_replay_natural_worker_runtime_v3.py': 11,
 'tests/test_replay_packet_cache_v1.py': 20,
 'tests/test_replay_video_web.py': 30,
 'tests/test_replay_video_worker_native.py': 3,
 'tests/test_replay_worker.py': 17,
 'tests/test_saved_draft_navigation_wait.py': 5,
 'tests/test_short_video_web.py': 115}

CACHE_RELEASE='cbb117c00e00cc25273f8110861e74a79b35b16b'
CACHE_PINS={'services/hub/replay_machine_checks.py':'1879757741d61173c9b5a27e6e3a210294b4c3efa9952e6b9483e97dd1fb6588',
 'services/hub/replay_packet_cache_v1.py':'7727417e5064cd669d416ce9cee4209caaddc8b3e84a59c490031ad5c6b2a2f1'}
ENTRY='services.hub.replay_natural_worker_runtime_v3'
RELEASE='aa1b6494bdde6898c90b656d934c0bd1ee418c13'
CHANNELS={'UCbbbbbbbbbbbbbbbbbbbbbb','UCaaaaaaaaaaaaaaaaaaaaaa'}
POLICIES=('youtube-highest-natural-download-policy.toml','replay-natural-video-web-policy.toml')
GRANT=Path('/etc/lighthouse-hub/replay-natural-workflow-launch-v3.json')
ACCEPTANCE=Path('/etc/lighthouse-hub/replay-natural-acceptance-v3/pilot.json')
SPECS={
 'natural':('replay-natural-worker-ubuntu-tests-v3','replay-natural-worker-ro-overlay-v3','replay-natural-worker-ro-overlay-v3-','lighthouse-replay-natural-owned-v3-',85,111,
  '5fb66ea8a9ba763e0816fa2358c4cdedfad55b95cd549dc5d30384f13c3cea38'),
 'download':('youtube-natural-readiness-v1d-ubuntu-tests','youtube-natural-readiness-v1d-ro-overlay','youtube-natural-readiness-v1d-','lighthouse-youtube-highest-owned-',280,66,
  '2ddfad8c1056c7c22eacbc81ae23da14d4be0527d2aa77d0c911c7df8ebf5fdb'),
 'integration':('natural-runtime-readiness-v3-ubuntu-tests','natural-runtime-readiness-v3-ro-overlay','natural-runtime-readiness-v3-ro-overlay-','lighthouse-natural-readiness-runtime-owned-v3-',21,116,
  '9169dbe158eb7e41818e5c303d84df0977ffb90e76d606f96565b49776d153e5'),
 'cache':('replay-packet-cache-v1-ubuntu-tests','replay-packet-cache-v1-ro-overlay','replay-packet-cache-v1-ro-overlay-',
  'lighthouse-replay-packet-cache-owned-v1-',295,119,'8fc11a3e27dca0dd2c08c12a34da111e70f37bc38ce74d6cdebb122840578e2a'),
 'native':('natural-navigation-wait-v3-345-ubuntu-tests-v1','natural-navigation-wait-v3-345-ro-overlay-v1',
  'natural-navigation-wait-v3-345-ro-overlay-','lighthouse-natural-navigation-wait-v3-345-owned-',345,124,TARGET_ARCHIVE)}
FLAGS={'youtube_natural_download_enabled':True,'youtube_download_acceptance_only':True,'resource_wait_max_hours':72,'youtube_readiness_max_hours':24}

def digest(raw):return hashlib.sha256(raw).hexdigest()
def protected(file):
    assert file.is_absolute() and file.resolve()==file
    for entry in (file,*file.parents):
        info=entry.lstat();assert not stat.S_ISLNK(info.st_mode) and info.st_uid==0 and not info.st_mode&0o022

def helper(file):
    file=Path(file);assert file.is_absolute() and file.resolve()==file and not file.is_symlink()
    raw=file.read_bytes();assert digest(raw)==HELPER_SHA
    result=types.ModuleType('pinned_highest_deployer');result.__file__=str(file);exec(compile(raw,str(file),'exec'),result.__dict__);return result

def cache_operator(file):
    file=Path(file);assert file.is_absolute() and file.resolve()==file and not file.is_symlink()
    raw=file.read_bytes();assert digest(raw)==CACHE_OPERATOR_SHA
    result=types.ModuleType('pinned_packet_cache_operator');result.__file__=str(file);exec(compile(raw,str(file),'exec'),result.__dict__);return result

def gate(path,sha,kind,d):
    version,mversion,prefix,ownedprefix,total,filecount,archive=SPECS[kind]
    path=d.safe(path);assert path.name=='test-attestation.json' and path.parent.parent==d.EV and path.parent.name.startswith(prefix)
    ident=path.parent.name[len(prefix):];assert str(uuid.UUID(ident))==ident
    assert re.fullmatch('[a-f0-9]{64}',sha) and digest(d.read(path,500000))==sha
    value=json.loads(d.read(path,500000));m=value['manifest']
    assert value['version']==version and value['image']==IMAGE and value['full_suite_passed'] is True
    assert value['archive_sha256']==archive and value['tests']==total and value['failures']==value['errors']==value['skipped']==value['exit_code']==0
    assert value['oom_killed'] is False and value['network']=='none'
    assert all(value[k] is True for k in ('runtime_readonly_overlays_only','runtime_matches_declared_candidate','mounts_match_isolated_allowlist','no_pid_or_oom_denial'))
    assert all(value[k] is False for k in ('whole_tree_deployed','application_data_mounted','production_profile_mounted','secrets_environment_injected'))
    assert m['version']==mversion and m['base_image']==IMAGE and m['expected_total_tests']==total and len(m['files'])==filecount
    assert m['expected_test_counts']==value['groups'] and sum(value['groups'].values())==total
    assert m['whole_tree_deployment'] is m['application_data_included'] is m['credentials_included'] is False
    owned=d.HOME/(ownedprefix+ident);raw=d.read(owned/'results.xml',20_000_000);assert digest(raw)==value['junit_sha256']
    cases=list(ET.fromstring(raw).iter('testcase'));counts={name:0 for name in value['groups']}
    assert len(cases)==total and all(not any(c.find(k) is not None for k in ('failure','error','skipped')) for c in cases)
    for c in cases:
        matched=[name for name in counts if c.attrib.get('classname','').endswith(Path(name).stem)];assert len(matched)==1;counts[matched[0]]+=1
    assert counts==value['groups'] and digest(d.read(path.parent/'tests.log',20_000_000))==value['tests_log_sha256']
    assert json.loads(d.read(path.parent/'overlay/manifest.json'))==m
    for name,record in m['files'].items():
        assert not Path(name).is_absolute() and '..' not in Path(name).parts
        raw=d.read(path.parent/'overlay'/name,500000);assert len(raw)==record['bytes'] and digest(raw)==record['sha256']
    container=json.loads(d.read(path.parent/'container.json',5_000_000));host=container['HostConfig']
    assert container['Id']==value['container_id'] and container['Image']==IMAGE and container['Config']['User']=='10001:10001'
    assert container['State']['ExitCode']==0 and container['State']['OOMKilled'] is False and host.get('Init') is True
    assert host['ReadonlyRootfs'] is True and host['NetworkMode']=='none' and host['PidsLimit']==256 and host['Memory']==1400*1024**2 and host['ShmSize']==256*1024**2
    expected={str(owned),str(path.parent/'check-and-test.py'),str(path.parent/'overlay/manifest.json')}|{str(path.parent/'overlay'/name) for name in m['files']}
    binds=[x for x in container['Mounts'] if x['Type']=='bind'];assert len(binds)==len(expected) and {x['Source'] for x in binds}==expected
    assert all(not x['RW'] or x['Source']==str(owned) for x in binds)
    assert all(x['Type']=='bind' or x['Type']=='tmpfs' and x['Destination']=='/tmp' for x in container['Mounts'])
    for label in ('before','after'):
        resources=json.loads(d.read(owned/('resource-'+label+'.json')));assert resources==value['resource_snapshot_'+label]
        assert int(resources['pids.events'].get('max',0))==int(resources['memory.events'].get('oom',0))==int(resources['memory.events'].get('oom_kill',0))==0
    return value,{'file':str(path),'sha256':sha,'tests':total}

def merged_sources(base,download,integration):
    old=base['manifest']['files'];new=integration['manifest']['files'];d=download['manifest']['files']
    assert integration['manifest']['source_freeze_commit']==download['manifest']['source_commit']==RELEASE
    assert integration['manifest']['previous_85_archive_sha256']==base['archive_sha256']
    assert set(old)<=set(new) and set(d)<=set(new)
    assert all(new[name]['sha256']==entry['sha256'] for name,entry in d.items())
    changed={name for name in new if name not in old or new[name]['sha256']!=old[name]['sha256']}
    assert changed==set(integration['manifest']['changed_from_85'])
    return new

def composed_sources(base,download,integration,cache):
    baseline=merged_sources(base,download,integration);manifest=cache['manifest'];new=manifest['files']
    assert manifest['source_freeze_commit']==CACHE_RELEASE
    assert manifest['parent_21_archive_sha256']==integration['archive_sha256']
    assert manifest['previous_85_archive_sha256']==base['archive_sha256']
    assert set(manifest['production_runtime_changed_files'])==set(CACHE_PINS)
    assert manifest['native_freshness_seconds_unchanged']==300 and manifest['native_observed_at_changed'] is False
    assert manifest['source_current_full_sha_required_each_call'] is True
    changed={name for name in new if name not in baseline or new[name]['sha256']!=baseline[name]['sha256']}
    allowed=set(CACHE_PINS)|{'tests/test_replay_machine_checks.py','tests/test_replay_packet_cache_v1.py','docs/REPLAY_PACKET_CACHE_V1.md'}
    assert set(baseline)<=set(new) and changed==set(manifest['changed_files'])==allowed
    assert all(new[name]['sha256']==sha for name,sha in CACHE_PINS.items())
    return new


def target_sources(baseline,native,d):
    m=native['manifest'];new=m['files']
    assert digest(d.read(Path(native['_attestation_file']).parent/'overlay/manifest.json',500000))==TARGET_MANIFEST_SHA
    assert m['parent_archive_sha256']==TARGET_PARENT and m['parent_expected_total_tests']==340
    assert m['navigation_source_commit']==NAV_COMMIT and m['python_source_commit']==PY_COMMIT and m['native_source_commit']==UI_COMMIT
    assert m['production_runtime_changed_files']==[NAV] and m['navigation_source_before_sha256']==NAV_OLD and m['navigation_source_after_sha256']==NAV_SHA
    assert m['guard_changed'] is m['browser_driver_changed'] is m['production_deployed'] is False
    assert m['native_freshness_seconds_unchanged']==300 and m['source_current_full_sha_required_each_call'] is True
    assert m['natural_sources_installed'] is True and native['groups']==TARGET_GROUPS
    assert set(baseline)<=set(new)
    changed={n for n in new if n not in baseline or new[n]['sha256']!=baseline[n]['sha256']}
    assert changed==set(TARGET_FROM_295) and all(new[n]['sha256']==s for n,s in TARGET_FROM_295.items())
    assert m['runtime_source_sha256']==TARGET_RUNTIME_PINS
    assert {n:e['sha256'] for n,e in new.items() if d.runtime_source(n)}==TARGET_RUNTIME_PINS
    assert native['mounted_sources_match_manifest'] is native['host_source_sha256_unchanged'] is True and native['timed_out'] is False
    wanted={n:e['sha256'] for n,e in new.items()}
    assert native['mounted_source_sha256_before']==native['mounted_source_sha256_after']==wanted
    return new

def current_qualification(kind,path,sha,d):
    assert kind in CURRENT_SPECS and kind not in SPECS and sha==CURRENT_ATTESTATIONS[kind]
    SPECS[kind]=CURRENT_SPECS[kind]
    try:value,reference=gate(path,sha,kind,d)
    finally:SPECS.pop(kind,None)
    m=value['manifest'];assert value['mounted_sources_match_manifest'] is value['host_source_sha256_unchanged'] is True and value['timed_out'] is False
    wanted={n:e['sha256'] for n,e in m['files'].items()}
    assert value['mounted_source_sha256_before']==value['mounted_source_sha256_after']==wanted
    if kind=='repair':
        assert m['python_source_commit']==PY_COMMIT and m['native_source_commit']==UI_COMMIT and m['natural_sources_installed'] is False
        assert m['parent_archive_sha256']=='fdea68bae719a0f20698558fd9601bc39bea930fd2000fbbbea1e26c370a843b'
        expected={**CURRENT_RUNTIME_PINS,NAV:NAV_OLD}
    else:
        assert m['production_runtime_changed_files']==[NAV] and m['navigation_source_commit']==NAV_COMMIT
        assert m['guard_changed'] is m['browser_driver_changed'] is m['production_deployed'] is False
        expected=CURRENT_RUNTIME_PINS
    assert m['runtime_source_sha256']==expected
    return value,reference

def describe_runtime_delta(current,target):
    assert current==CURRENT_RUNTIME_PINS and target==TARGET_RUNTIME_PINS
    return {'shared_source_changes':{n:{'before_sha256':current[n],'after_sha256':target[n]} for n in sorted(set(current)&set(target)) if current[n]!=target[n]},
        'added_sources':{n:target[n] for n in sorted(set(target)-set(current))},
        'removed_sources':{n:current[n] for n in sorted(set(current)-set(target))},
        'current_count':len(current),'target_count':len(target),'navigation_already_matches':current[NAV]==target[NAV]}

def current_gate(d,before,current,legacy,operator,mixed,repair,navigation):
    assert repair['manifest']['runtime_source_sha256']=={**CURRENT_RUNTIME_PINS,NAV:NAV_OLD}
    assert navigation['manifest']['runtime_source_sha256']==CURRENT_RUNTIME_PINS
    old=mixed['manifest']['candidate_runtime_source_sha256'];repaired=repair['manifest']['runtime_source_sha256']
    expected_repair=set(TARGET_FROM_295)-{NAV,'tests/metadata_v5_async_fixture.mjs','tests/saved_draft_navigation_wait_fixture.mjs',
        'tests/test_metadata_v5_async.py','tests/test_replay_native_snapshot_delta.py','tests/test_saved_draft_navigation_wait.py'}
    assert set(old)==set(repaired) and {n for n in old if old[n]!=repaired[n]}==expected_repair
    service=deepcopy(before['services']['video-worker'])
    assert service['command']==['python','-m','services.hub.replay_highest_moderation_v2b']
    for v in service['volumes']:v.setdefault('read_only',False)
    operator.validate_running(d,current,service)
    volumes=service['volumes'];assert len({v['target'] for v in volumes})==len(volumes)
    prefix=str(d.ROOT)+'/'
    code={v['target'][len(prefix):]:v for v in volumes if v['target'].startswith(prefix) and d.runtime_source(v['target'][len(prefix):])}
    assert set(code)==set(CURRENT_RUNTIME_PINS)
    for name,expected in CURRENT_RUNTIME_PINS.items():
        v=code[name];file=Path(v['source']);protected(file)
        assert v['type']=='bind' and v['read_only'] is True and digest(d.read(file,500000))==expected
        assert file.is_relative_to(d.INSTALL) or file.is_relative_to(Path('/usr/local/lib/lighthouse-saved-draft-navigation-v3'))
    observed=json.loads(d.run(['/usr/bin/docker','exec','--user','10001:10001',current['Id'],'python','-c',
        'from pathlib import Path;import hashlib,json;print(json.dumps({n:hashlib.sha256((Path('+repr(str(d.ROOT))+')/n).read_bytes()).hexdigest() for n in '+repr(list(CURRENT_RUNTIME_PINS))+'}))']))
    assert observed==CURRENT_RUNTIME_PINS
    verify_runtime_environment(d,current['Id'],operator)

def preserved_tweet_layout(d,before):
    wanted=str(d.ROOT/'config/tweet-runtime-v4.toml')
    for name in ('api','tweet-worker'):
        assert before['services'][name]['environment']['HUB_CONFIG']==wanted
    mounted=[v for v in before['services']['scheduler']['volumes'] if v['target']==str(d.ROOT/'scheduler/service.py')]
    assert len(mounted)==1 and mounted[0]['type']=='bind' and mounted[0].get('read_only') is True
    file=Path(mounted[0]['source']);protected(file);assert digest(d.read(file,500000))==SCHEDULER_SHA
    return {'api_and_tweet_config':wanted,'scheduler_source_sha256':SCHEDULER_SHA,'other_service_objects_preserved':True}

def proposed_service(d,key,code,code_root,operator):
    service=d.new_video_service(key,code,code_root=code_root);service['command']=['python','-m',ENTRY]
    targets=dict(item.split(':',1) for item in service['tmpfs']);assert not set(targets)&set(operator.DIRECTORIES)
    service['tmpfs'].extend(path+':'+value for path,value in operator.DIRECTORIES.items())
    service['volumes']+=[d.bind(GRANT,GRANT,True),d.bind(ACCEPTANCE,ACCEPTANCE,True)]
    return service

def verify_runtime_environment(d,container_id,operator):
    value=json.loads(d.run(['/usr/bin/docker','exec','--user','10001:10001',container_id,'python','-c',operator.VERIFY_ENV]))
    assert value['persistent_runtime_environment_directories_verified'] is True
    return value

def candidate_hub(raw):
    text=raw.decode('utf-8-sig');before=tomllib.loads(text);lines=text.splitlines(keepends=True);ending='\r\n' if '\r\n' in text else '\n'
    boundary=next((i for i,line in enumerate(lines) if line.lstrip().startswith('[')),len(lines));head=lines[:boundary];tail=lines[boundary:]
    for name,value in FLAGS.items():
        matched=[i for i,line in enumerate(head) if re.match(r'^\s*'+re.escape(name)+r'\s*=',line)]
        assert len(matched)<=1;encoded='true' if value is True else str(value)
        if matched:
            i=matched[0];head[i]=re.sub(r'(^\s*'+re.escape(name)+r'\s*=\s*)(?:true|false|[0-9]+)',lambda m:m[1]+encoded,head[i],count=1)
        else:
            if head and not head[-1].endswith(('\n','\r')):head[-1]+=ending
            head.append(name+' = '+encoded+ending)
    output=''.join(head+tail);expected=deepcopy(before);expected.update(FLAGS);assert tomllib.loads(output)==expected
    return (b'\xef\xbb\xbf' if raw.startswith(b'\xef\xbb\xbf') else b'')+output.encode()

def stage(directory,d):
    directory=d.safe(directory);protected(directory);assert directory.is_dir()
    sources={name:directory/name for name in POLICIES};sources.update(grant=directory/'workflow-launch.json',snapshot=directory/'pilot.json')
    bodies={}
    for name,file in sources.items():
        protected(file);info=file.stat();assert info.st_gid==10001 and stat.S_IMODE(info.st_mode)==0o640
        bodies[name]=d.read(file,1048576 if name=='snapshot' else 65536)
    download,replay=(tomllib.loads(bodies[name].decode('utf-8-sig')) for name in POLICIES)
    grant=json.loads(bodies['grant']);snapshot=json.loads(bodies['snapshot'])
    start=datetime.fromisoformat(download['activated_at'].replace('Z','+00:00'));other=datetime.fromisoformat(replay['activated_at'].replace('Z','+00:00'))
    assert start.tzinfo is not None and start==other and set(download['allowed_channel_ids'])==set(replay['allowed_channel_ids'])==CHANNELS
    assert download['daily_http_requests']==6000 and download['monthly_http_requests']==150000
    assert download['enabled'] is replay['enabled'] is replay['automation_enabled'] is True
    assert download['operating_not_before']==replay['operating_not_before']=='2026-10-31T16:00:00Z'
    assert grant['policy_sha256']==digest(bodies[POLICIES[1]]) and grant['pilot_acceptance_snapshot_sha256']==digest(bodies['snapshot'])
    assert snapshot['accepted_at']==grant['accepted_at'] and start.timestamp()<=datetime.fromisoformat(grant['accepted_at'].replace('Z','+00:00')).timestamp()
    return sources,bodies,start.timestamp()

PRECHECK=r'''
from pathlib import Path
import json,os,socket,threading
from services.hub.config import Settings
from services.hub.db import Store
from services.hub.worker import Worker
from services.hub.replay_moderation_common import QueueView
def refuse(*args,**kwargs):raise RuntimeError('NATURAL_PREFLIGHT_CONSUMPTION_OR_NETWORK_FORBIDDEN')
socket.socket.connect=refuse;Store.claim=Store.recover=Store.enqueue=Store.finish=refuse
Worker.once=Worker.run=QueueView.claim=QueueView.recover=threading.Thread.start=refuse
from services.hub.replay_natural_worker_runtime_v3 import build_worker
from services.hub.replay_worker_runtime import load_youtube_key
assert os.getuid()==os.getgid()==10001
load_youtube_key('/run/secrets/youtube-api-key')
s=Settings.load();db=Store('/run/preflight-db/hub.sqlite3');db.init()
worker,service,downloader=build_worker(s,db)
worker.resource_waits.initialize();worker.readiness_waits.initialize()
assert {'youtube_highest_prepare_v3','youtube_download_v3','replay_natural_video_publish_v3','youtube_highest_readiness_observation_v3'}<=set(worker.handlers)
assert db.query('PRAGMA integrity_check')[0]['integrity_check']=='ok' and not db.query('PRAGMA foreign_key_check')
print(json.dumps({'ready':True,'policy_sha256':service.policy()['_sha256'],'claim_called':False,'network_enabled':False,'only_backup_initialized':True}))
'''

def preflight(d,overlay,code,key,sources,hub,operator):
    command=['/usr/bin/docker','run','--rm','--pull=never','--network=none','--read-only','--user','10001:10001','--cap-drop=ALL','--security-opt','no-new-privileges:true',
        '--init','--pids-limit=256','--memory=1400m','--shm-size=256m','--tmpfs','/tmp:rw,size=192m,mode=1777','-e','HUB_ROOT='+str(d.ROOT),
        '-e','HUB_CONFIG='+str(d.ROOT/'config/hub.toml'),'-e','HOME=/tmp/highest/home',
        '-e','XDG_CONFIG_HOME=/tmp/highest/config','-e','XDG_CACHE_HOME=/tmp/highest/cache','-e','PYTHONDONTWRITEBYTECODE=1']
    for path,value in operator.DIRECTORIES.items():command+=['--tmpfs',path+':'+value]
    mounts=[(d.ROOT/'config',d.ROOT/'config'),(key,'/run/secrets/youtube-api-key'),(sources['grant'],GRANT),(sources['snapshot'],ACCEPTANCE)]
    mounts.extend((sources[name],d.ROOT/'config'/name) for name in POLICIES)
    mounts.extend((overlay/name,d.ROOT/name) for name in code)
    with tempfile.TemporaryDirectory(prefix='lighthouse-natural-preflight-',dir='/run') as folder:
        folder=Path(folder);folder.chmod(0o755);dbfolder=folder/'database';dbfolder.mkdir(mode=0o700)
        file,backup=d.online_snapshot(dbfolder);os.chown(file,10001,10001);file.chmod(0o600);os.chown(dbfolder,10001,10001);dbfolder.chmod(0o700)
        candidate=folder/'hub.toml';candidate.write_bytes(hub);os.chown(candidate,0,10001);candidate.chmod(0o640);mounts.append((candidate,d.ROOT/'config/hub.toml'))
        for source,target in mounts:command+=['--mount','type=bind,source='+str(source)+',target='+str(target)+',readonly']
        command+=['--mount','type=bind,source='+str(dbfolder)+',target=/run/preflight-db']
        check=operator.VERIFY_ENV.split('print(json.dumps')[0]+'\n'+PRECHECK
        result=json.loads(d.run(command+[IMAGE,'python','-c',check],timeout=90));assert result['ready'] is True
        result['database_snapshot']=backup
    return result

QUERY=r'''
from pathlib import Path
import json,os,sqlite3,sys
assert os.getuid()==10001
db=sqlite3.connect(Path(sys.argv[1]).as_uri()+'?mode=ro',uri=True);db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
def scalar(sql,args=()):return db.execute(sql,args).fetchone()[0]
counts={'video_processing':scalar("SELECT COUNT(*) FROM jobs WHERE queue='video' AND state='processing'"),
 'old_lookup_runnable':scalar("SELECT COUNT(*) FROM jobs WHERE job_type='replay_highest_moderation_lookup_v2b' AND state IN ('pending','processing','retry_scheduled')"),
 'old_unresolved_publish':scalar("SELECT COUNT(*) FROM jobs WHERE job_type='replay_highest_video_publish_v2' AND state NOT IN ('completed','failed')"),
 'natural_jobs':scalar("SELECT COUNT(*) FROM jobs WHERE job_type IN ('youtube_highest_prepare_v3','youtube_download_v3','replay_natural_video_publish_v3','youtube_highest_readiness_observation_v3','replay_natural_moderation_lookup_v3')"),
 'pilot_completed':scalar("SELECT COUNT(*) FROM jobs WHERE job_id=? AND state='completed' AND job_type='replay_highest_video_publish_v2'",(sys.argv[2],))}
counts['heartbeats']={name:at for name,at in db.execute("SELECT name,at FROM heartbeats WHERE name IN ('worker:video','worker:video-main-v3','worker:video-moderation-v3')")}
print(json.dumps(counts));db.close()
'''

def database_status(d,pilot):
    def app():os.setgroups([]);os.setgid(10001);os.setuid(10001)
    result=subprocess.run(['/usr/bin/python3','-c',QUERY,str(d.ROOT/'data/hub.sqlite3'),pilot],capture_output=True,text=True,timeout=20,
        preexec_fn=app,cwd='/tmp',env={'PATH':'/usr/bin:/bin','HOME':'/tmp','PYTHONNOUSERSITE':'1'})
    assert result.returncode==0;return json.loads(result.stdout)

def idle(status):return status['video_processing']==status['old_lookup_runnable']==status['old_unresolved_publish']==0 and status['pilot_completed']==1

def core_processes(d):
    result={}
    for name in d.CORE:
        value=d.inspect(d.container_id(name));assert value['State']['Running'] and not value['State']['OOMKilled']
        result[name]={'id':value['Id'],'pid':value['State']['Pid'],'started_at':value['State']['StartedAt']}
    return result



def install_file(d,file,body,*,mode=0o640,group=10001):
    # Reject a bad pre-existing ancestor before mkdir/chmod can follow it.
    d.safe(file)
    for ancestor in file.parents:
        if ancestor.exists():protected(ancestor)
    file.parent.mkdir(parents=True,mode=0o750,exist_ok=True)
    for directory in (file.parent,*file.parent.parents):
        if directory in (Path('/'),Path('/etc'),Path('/opt'),Path('/usr'),Path('/usr/local'),Path('/usr/local/lib')):break
        info=directory.lstat();assert info.st_uid==0 and not stat.S_ISLNK(info.st_mode) and not info.st_mode&0o022
        if directory.is_relative_to(d.INSTALL):directory.chmod(0o755)
        elif directory.is_relative_to(Path('/etc/lighthouse-hub')):os.chown(directory,0,10001);directory.chmod(0o750)
    d.atomic(file,body,mode=mode);os.chown(file,0,group);file.chmod(mode);protected(file)
    for path in (file,file.parent):
        fd=os.open(path,os.O_RDONLY|(os.O_DIRECTORY if path.is_dir() else 0))
        try:os.fsync(fd)
        finally:os.close(fd)

def role_lock(d,name):
    """Open app-data locks through directory descriptors, never root chown
    through an app-writable symlink. Existing ordinary locks stay untouched.
    """
    assert name in ('video','moderation');protected(d.ROOT)
    flags=os.O_RDONLY|getattr(os,'O_DIRECTORY',0)|getattr(os,'O_NOFOLLOW',0);opened=[];returned=None
    try:
        root=os.open(d.ROOT,flags);opened.append(root)
        data=os.open('data',flags,dir_fd=root);opened.append(data)
        locks=os.open('locks',flags,dir_fd=data);opened.append(locks)
        created=False
        try:os.mkdir(name,mode=0o700,dir_fd=locks);created=True
        except FileExistsError:pass
        folder=os.open(name,flags,dir_fd=locks);opened.append(folder);info=os.fstat(folder)
        if created:
            assert info.st_uid==0;os.fchown(folder,10001,10001);os.fchmod(folder,0o700)
        else:assert info.st_uid==10001 and not info.st_mode&0o022
        try:lock=os.open('observer.lock',os.O_RDONLY|getattr(os,'O_NOFOLLOW',0),dir_fd=folder)
        except FileNotFoundError:
            lock=os.open('observer.lock',os.O_RDWR|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600,dir_fd=folder)
            os.fchown(lock,10001,10001);os.fchmod(lock,0o600)
        opened.append(lock);info=os.fstat(lock)
        assert stat.S_ISREG(info.st_mode) and info.st_uid==10001 and not info.st_mode&0o022 and info.st_nlink==1
        returned=lock;return lock
    finally:
        for fd in reversed(opened):
            if fd!=returned:os.close(fd)

def rollback_if_safe(d,pilot,backups,original,compose_stat,processes,*,locks_held=False):
    """Stop only under both idle role locks; recheck admissions after stop.

    Producers run before Worker.once, so a pre-stop job-count check alone is
    insufficient. If a producer admitted work during shutdown, resume the
    new service and retain all new configuration instead of exposing that
    job to the old consumer.
    """
    descriptors=[]
    try:
        if not locks_held:
            import fcntl
            for name in ('video','moderation'):
                fd=role_lock(d,name)
                try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:os.close(fd);return False
                descriptors.append(fd)
        before=database_status(d,pilot)
        if before['natural_jobs'] or before['video_processing'] or core_processes(d)!=processes:return False
        d.compose('stop','--timeout','45','video-worker')
        after=database_status(d,pilot)
        if after['natural_jobs'] or after['video_processing']:
            d.compose('up','-d','--no-deps','--no-build','video-worker');return False
        for file,previous in backups.items():
            if previous is None:file.unlink(missing_ok=True)
            else:d.atomic(file,previous[0],original=previous[1])
        d.atomic(d.ROOT/'compose.yaml',original,original=compose_stat);d.compose('up','-d','--no-deps','--no-build','video-worker')
        return True
    finally:
        for fd in reversed(descriptors):os.close(fd)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for kind in ('natural','download','integration','legacy','cache','mix','native','repair','navigation'):
        parser.add_argument('--'+kind+'-attestation',required=True);parser.add_argument('--'+kind+'-sha256',required=True)
    parser.add_argument('--candidate-directory',required=True);parser.add_argument('--deployer-file',required=True)
    parser.add_argument('--cache-operator-file',required=True);parser.add_argument('--activate',action='store_true')
    args=parser.parse_args();assert os.name=='posix' and os.geteuid()==0;d=helper(args.deployer_file)
    operator=cache_operator(args.cache_operator_file)
    values={};references={}
    for kind in SPECS:values[kind],references[kind]=gate(Path(getattr(args,kind+'_attestation')),getattr(args,kind+'_sha256'),kind,d)
    assert args.native_sha256==TARGET_ATTESTATION_SHA
    values['native']['_attestation_file']=args.native_attestation
    for kind in CURRENT_SPECS:values[kind],references[kind]=current_qualification(kind,Path(getattr(args,kind+'_attestation')),getattr(args,kind+'_sha256'),d)
    legacy,lref=d.gate(Path(args.legacy_attestation),args.legacy_sha256);references['legacy']=lref
    mixed,mref=operator.mixed_gate(Path(args.mix_attestation),args.mix_sha256,types.SimpleNamespace(SPECS=SPECS,gate=gate),d)
    references['current_exact_v2_mix']=mref
    baseline=composed_sources(values['natural'],values['download'],values['integration'],values['cache'])
    files=target_sources(baseline,values['native'],d)
    assert values['cache']['manifest']['previous_280_manifest_sha256']==digest(d.read(Path(args.download_attestation).parent/'overlay/manifest.json',500000))
    overlay=Path(args.native_attestation).parent/'overlay'
    code={name:d.read(overlay/name,500000) for name in files if d.runtime_source(name)}
    assert all(digest(body)==files[name]['sha256'] for name,body in code.items()) and 45<len(code)<70
    sources,bodies,activation=stage(Path(args.candidate_directory),d);pilot=json.loads(bodies['grant'])['acceptance_job_id']
    protected(d.ROOT/'compose.yaml');original=d.read(d.ROOT/'compose.yaml');compose_stat=(d.ROOT/'compose.yaml').stat();before=d.compose_config();current=d.inspect(d.container_id('video-worker'))
    already=before['services']['video-worker']['command']==['python','-m',ENTRY]
    tweet_layout=preserved_tweet_layout(d,before)
    if not already:current_gate(d,before,current,legacy,operator,mixed,values['repair'],values['navigation'])
    processes=core_processes(d)
    key=[v['source'] for v in before['services']['video-worker']['volumes'] if v['target']=='/run/secrets/youtube-api-key'];assert len(key)==1;key=d.key_file(key[0])
    hub_file=d.ROOT/'config/hub.toml';protected(hub_file);hub=d.read(hub_file);hub_candidate=candidate_hub(hub)
    if not already:assert digest(hub)==GLOBAL_HUB_SHA
    prepared=preflight(d,overlay,code,key,sources,hub_candidate,operator)
    status=database_status(d,pilot);assert status['pilot_completed']==1
    code_root=d.INSTALL/'natural-v3c-native'/TARGET_ARCHIVE/'code'
    service=proposed_service(d,key,code,code_root,operator)
    proposed=d.candidate_config(before,service)
    report={'version':'natural-replay-v3c-native-deployment-v1','state':'verified_plan_only','references':references,'runtime_sources':len(code),
        'inherited_download_release_commit':RELEASE,'native_source_commits':[PY_COMMIT,UI_COMMIT,NAV_COMMIT],
        'qualified_native_archive_sha256':TARGET_ARCHIVE,'runtime_delta':describe_runtime_delta(CURRENT_RUNTIME_PINS,TARGET_RUNTIME_PINS),'preserved_tweet_layout':tweet_layout,
        'activation_blockers':list(KNOWN_ACTIVATION_BLOCKERS),'preflight':prepared,'database_before':status,'other_core_processes':processes,'activation_at':datetime.fromtimestamp(activation,timezone.utc).isoformat(),
        'candidate_hub_sha256':digest(hub_candidate),'candidate_policy_sha256':{name:digest(bodies[name]) for name in POLICIES},
        'grant_sha256':digest(bodies['grant']),'snapshot_sha256':digest(bodies['snapshot']),'jobs_reset':False,'jobs_created_by_installer':0,
        'activated':False,'new_paid_services':False,'automatic_history_backfill':False,'top_level_config_values_changed':FLAGS,
        'current_runtime_qualification':'A68-plus-navigation5','legacy_cache_operator_role':'strict-tmpfs-and-legacy-qualification-only',
        'navigation_host_operator_sha256':'118189718b4b41e0728db3eb383c93e1c1fa2dc99e585803d9c571d9adbc398e',
        'current_operator_commit':CURRENT_OPERATOR_COMMIT,'cache_operator_sha256':CACHE_OPERATOR_SHA,
        'cache_source_commit':CACHE_RELEASE,'cache_delta_runtime_files':list(CACHE_PINS),'independent_test_scopes_not_summed':True,
        'native_freshness_seconds_unchanged':300,'bounded_runtime_directories':operator.DIRECTORIES}
    if already:
        normalized=deepcopy(service)
        for volume in normalized['volumes']:volume.setdefault('read_only',False)
        operator.validate_running(d,current,normalized)
        verify_runtime_environment(d,current['Id'],operator)
        assert hub==hub_candidate and all(d.read(d.ROOT/'config'/name,65536)==bodies[name] for name in POLICIES)
        assert d.read(GRANT,65536)==bodies['grant'] and d.read(ACCEPTANCE,1048576)==bodies['snapshot']
        for name,body in code.items():protected(code_root/name);assert d.read(code_root/name,500000)==body
        report.update(state='already_applied_verified',activated=True,video_container_id=current['Id']);print(json.dumps(report));return
    if not args.activate:print(json.dumps(report));return
    if KNOWN_ACTIVATION_BLOCKERS:
        report['state']='blocked_known_producer_starvation';print(json.dumps(report))
        raise RuntimeError('NATURAL_V3C_ACTIVATION_REQUIRES_QUALIFIED_QUEUE_FIX')
    assert 0<=time.time()-activation<=300 # A stale plan is not 'activate now'.
    import fcntl
    globalfd=os.open('/run/lighthouse-natural-replay-v3-deploy.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600);fcntl.flock(globalfd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    descriptors=[];keep_global=False;cutover_started=False
    try:
        for name in ('video','moderation'):
            fd=role_lock(d,name)
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:os.close(fd);report['state']='deferred_active_video_or_lookup';print(json.dumps(report));return
            descriptors.append(fd)
        fresh=database_status(d,pilot)
        if not idle(fresh):report['state']='deferred_unresolved_or_processing_video';print(json.dumps(report));return
        assert fresh['natural_jobs']==0 and d.read(d.ROOT/'compose.yaml')==original and core_processes(d)==processes
        # Reopen private staging after preflight; never install changed bytes.
        for name,file in sources.items():assert d.read(file,1048576)==bodies[name]
        evidence=d.EV/('natural-replay-v3c-native-deploy-'+str(uuid.uuid4()));d.private_directory(evidence)
        d.atomic(evidence/'compose-before.yaml',original);d.atomic(evidence/'video-before.json',d.wire(d.filtered(current)))
        targets={hub_file:hub_candidate,**{d.ROOT/'config'/name:bodies[name] for name in POLICIES},GRANT:bodies['grant'],ACCEPTANCE:bodies['snapshot']}
        backups={}
        for i,(file,body) in enumerate(targets.items()):
            if file.exists():protected(file);backups[file]=(d.read(file,1048576),file.stat());d.atomic(evidence/('config-before-'+str(i)),backups[file][0])
            else:backups[file]=None
        for name,body in code.items():
            file=code_root/name
            if file.exists():protected(file);assert d.read(file,500000)==body
            else:install_file(d,file,body,mode=0o644,group=0)
        d.atomic(evidence/'runtime-source-manifest.json',d.wire({name:digest(body) for name,body in code.items()}))
        cutover_started=True;d.compose('stop','--timeout','45','video-worker');assert not d.inspect(current['Id'])['State']['Running'];assert idle(database_status(d,pilot))
        for file,body in targets.items():install_file(d,file,body)
        d.atomic(d.ROOT/'compose.yaml',d.wire(proposed),original=compose_stat);d.assert_other_services(before,d.compose_config())
        started=time.time();d.compose('up','-d','--no-deps','--no-build','video-worker');updated=d.inspect(d.container_id('video-worker'));keep_global=True
    except BaseException:
        report.update(state='requires_reconciliation',automatic_rollback_performed=False)
        if cutover_started and 'backups' in locals():
            try:
                if rollback_if_safe(d,pilot,backups,original,compose_stat,processes,locks_held=True):report.update(state='rolled_back_no_natural_jobs',automatic_rollback_performed=True)
            except BaseException:report['rollback_error']='ROLLBACK_REQUIRES_OPERATOR_RECONCILIATION'
        if 'evidence' in locals():d.atomic(evidence/'deployment-report.json',d.wire(report))
        print(json.dumps(report));raise RuntimeError('NATURAL_DEPLOY_REQUIRES_RECONCILIATION') from None
    finally:
        for fd in reversed(descriptors):os.close(fd)
        if not keep_global:os.close(globalfd)
    try:
        expected=deepcopy(service)
        for volume in expected['volumes']:volume.setdefault('read_only',False)
        operator.validate_running(d,updated,expected)
        verify_runtime_environment(d,updated['Id'],operator)
        assert all(digest(d.read(code_root/name,500000))==digest(body) for name,body in code.items())
        assert all(d.read(file,1048576)==body for file,body in targets.items())
        deadline=time.monotonic()+60
        while True:
            fresh=database_status(d,pilot);beats=fresh['heartbeats']
            if all(beats.get(name,0)>=started for name in ('worker:video','worker:video-main-v3','worker:video-moderation-v3')):break
            assert d.inspect(updated['Id'])['State']['Running'] and time.monotonic()<deadline;time.sleep(1)
        with urllib.request.urlopen('http://127.0.0.1:8080/ready',timeout=5) as response:assert response.status==200
        assert core_processes(d)==processes;d.assert_other_services(before,d.compose_config())
        report.update(state='active',activated=True,video_container_id=updated['Id'],actual_service_started_at=updated['State']['StartedAt'],ready=True,other_core_processes_unchanged=True)
    except BaseException:
        report.update(state='requires_reconciliation',automatic_rollback_performed=False)
        # A new admitted job makes an old consumer unsafe; never reset it or
        # stop its live work to manufacture a rollback.
        try:
            if rollback_if_safe(d,pilot,backups,original,compose_stat,processes):report.update(state='rolled_back_no_natural_jobs',automatic_rollback_performed=True)
        except BaseException:report['rollback_error']='ROLLBACK_REQUIRES_OPERATOR_RECONCILIATION'
        d.atomic(evidence/'deployment-report.json',d.wire(report));os.close(globalfd);print(json.dumps(report));raise RuntimeError('NATURAL_DEPLOY_NOT_ACCEPTED') from None
    d.atomic(evidence/'video-after.json',d.wire(d.filtered(updated)));d.atomic(evidence/'deployment-report.json',d.wire(report));os.close(globalfd);print(json.dumps(report))

if __name__=='__main__':main()
