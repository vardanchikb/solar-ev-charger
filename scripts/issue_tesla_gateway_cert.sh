#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_PATH="${1:-$ROOT_DIR/config.yaml}"

eval "$(python3 - <<'PY' "$CONFIG_PATH"
import os, sys, yaml
cfg_path = os.path.abspath(sys.argv[1])
base = os.path.dirname(cfg_path)
cfg = yaml.safe_load(open(cfg_path))["tesla_energy"]
domain = cfg.get("partner_domain", "").strip()
static_root = cfg.get("public_static_root", "/var/www/html")
cert = cfg.get("tls_cert_file", os.path.join(base, ".secrets", "tls", domain, "fullchain.pem"))
key = cfg.get("tls_key_file", os.path.join(base, ".secrets", "tls", domain, "privkey.pem"))
print(f'DOMAIN={domain!r}')
print(f'STATIC_ROOT={static_root!r}')
print(f'TLS_CERT_FILE={cert!r}')
print(f'TLS_KEY_FILE={key!r}')
PY
)"

if [[ -z "${DOMAIN}" ]]; then
  echo "tesla_energy.partner_domain is not configured" >&2
  exit 1
fi

mkdir -p "$(dirname "$TLS_CERT_FILE")"

"$HOME/.acme.sh/acme.sh" --issue -d "$DOMAIN" -w "$STATIC_ROOT" --server letsencrypt
"$HOME/.acme.sh/acme.sh" --install-cert -d "$DOMAIN" \
  --key-file "$TLS_KEY_FILE" \
  --fullchain-file "$TLS_CERT_FILE"

systemctl --user restart carcharger-tesla-public-gateway.service
echo "Installed TLS cert for $DOMAIN"
