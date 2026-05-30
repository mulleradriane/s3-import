#!/usr/bin/env python3
"""Regenera CHANGES.md e _import_commands.sh a partir de .s3configs cache."""
import json
import sys
from pathlib import Path

from s3_main_tf_gen import gen_changes_md, gen_import_commands, extract_logical

BASE = Path(__file__).parent / 'mr_output/ecs-ecred-default-aws-terraform/services/s3'
CONFIGS = Path(__file__).parent / 'mr_output/.s3configs'
STATE_BUCKET = '387979423286-tfstate'
LOG_BUCKET = 'ecs-387979423286-logging-s3'

SERVICES = [
'consumer-file', 'platform-bucket',
'integration-platform-partners', 'integration-platform-web',
'nogordio', 'platform-error-reprocess', 'simulation-engine', 'web-files',
]

def main():
for svc in SERVICES:
bucket = f'ecs-ecred-{svc}-dev'
cfg_path = CONFIGS / f'{bucket}.s3config.json'
if not cfg_path.exists():
print(f'SKIP {svc}: sem {cfg_path}', file=sys.stderr)
continue
cfg = json.loads(cfg_path.read_text())
logical, _ = extract_logical(bucket, 'ecred', 'dev')
repo = 'ecs-ecred-default-aws-terraform'
state_key = f'{repo}/services/s3/{logical}/dev/terraform.tfstate'
out = BASE / svc / 'dev'
out.mkdir(parents=True, exist_ok=True)
(out / 'CHANGES.md').write_text(
gen_changes_md(bucket, 'ecred', 'dev', 'Development', cfg, LOG_BUCKET),
encoding='utf-8',
)
(out / '_import_commands.sh').write_text(
gen_import_commands(bucket, cfg, state_key, env='dev', asset_cat='Development'),
encoding='utf-8',
)
print(f'OK {svc}')
return 0

if __name__ == '__main__':
sys.exit(main())




