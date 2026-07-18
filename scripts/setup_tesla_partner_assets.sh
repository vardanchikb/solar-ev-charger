#!/usr/bin/env bash
set -euo pipefail

DOMAIN="${1:?Usage: setup_tesla_partner_assets.sh <partner-domain> [www-appspecific-dir]}"
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SECRET_DIR="$BASE_DIR/.secrets"
WWW_DIR="${2:-/var/www/html/.well-known/appspecific}"

mkdir -p "$SECRET_DIR"
mkdir -p "$WWW_DIR"

PRIVATE_KEY="$SECRET_DIR/tesla_partner_private_key.pem"
PUBLIC_KEY="$WWW_DIR/com.tesla.3p.public-key.pem"

if [[ ! -f "$PRIVATE_KEY" ]]; then
  openssl ecparam -name prime256v1 -genkey -noout -out "$PRIVATE_KEY"
  chmod 600 "$PRIVATE_KEY"
fi

openssl ec -in "$PRIVATE_KEY" -pubout -out "$PUBLIC_KEY"
chmod 644 "$PUBLIC_KEY"

echo "Generated Tesla partner keypair for $DOMAIN"
echo "Private key: $PRIVATE_KEY"
echo "Public key:  $PUBLIC_KEY"
