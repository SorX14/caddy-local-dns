set shell := ["bash", "-euo", "pipefail", "-c"]

cert := "caddy_data/caddy/pki/authorities/local/root.crt"
ca_name := "Caddy Local Authority"
keychain := "/Library/Keychains/System.keychain"
resolver_dir := env("RESOLVER_DIR", "/private/etc/resolver")

# List available recipes
default:
    @just --list

# Create the shared network, prepare the DNS image, start Caddy and DNS, and offer to trust the root CA
up: network _resolver-dir _image
    docker compose up -d
    @just _maybe-trust

# Build the dns image locally instead of pulling it (for working on ./dns)
build:
    docker compose build dns

# Stop Caddy and DNS, and drop the resolver files even if Docker couldn't be reached
down:
    #!/usr/bin/env bash
    status=0
    docker compose down || status=$?
    just dns-clear
    exit $status

# Restart everything (picks up Caddyfile changes)
restart: down up

# Follow logs (all services, or the named ones)
logs *services:
    docker compose logs -f {{services}}

# Show container status
ps:
    docker compose ps

# Print the Caddyfile generated from container labels
show-caddyfile:
    docker compose exec caddy cat /config/caddy/Caddyfile.autosave

# Run all the Python tests; pass `unit` or `integration` to run just that group
test group="":
    python3 -B -m unittest discover -t . -s tests{{ if group == "" { "" } else { "/" + group } }}

# Check for the usual problems and say how to fix them (works even when the routes page is down)
doctor:
    docker compose exec dns-web python3 /app/diagnostics.py

# Clear macOS's DNS cache, for a name stuck on an old or wrong address (password required)
flush-dns:
    sudo dscacheutil -flushcache
    sudo killall -HUP mDNSResponder

# Remove the resolver files this setup created (also done on `down`, and by dns-sync on shutdown)
dns-clear:
    #!/usr/bin/env bash
    set -euo pipefail
    for name in $(just domains); do
        rm -f "{{resolver_dir}}/$name"
    done

# List the domains currently resolving to localhost
domains:
    #!/usr/bin/env bash
    set -euo pipefail
    shopt -s nullglob
    for file in "{{resolver_dir}}"/*; do
        if [[ -f $file && ! -L $file && $(head -n 1 "$file") == "# managed by caddy-local" ]]; then
            basename "$file"
        fi
    done

# Create the external `caddy` network if it doesn't exist
network:
    @docker network inspect caddy >/dev/null 2>&1 || docker network create caddy --ipv6

# Add this Caddy's root CA to the macOS system keychain, replacing any other Caddy CA
trust: _wait-for-cert
    #!/usr/bin/env bash
    set -euo pipefail
    just untrust
    echo "Adding certificate to keychain (password required):"
    sudo security add-trusted-cert -d -r trustRoot -k "{{keychain}}" "{{cert}}"

# Remove every Caddy root CA from the macOS system keychain
untrust:
    #!/usr/bin/env bash
    set -euo pipefail
    for hash in $(security find-certificate -a -Z -c "{{ca_name}}" "{{keychain}}" 2>/dev/null | awk '/SHA-1 hash:/ {print $3}'); do
        echo "Removing existing certificate $hash (password required):"
        sudo security delete-certificate -Z "$hash" "{{keychain}}"
    done

# Build locally by default, or pull DNS_IMAGE with a local build as fallback
_image:
	#!/usr/bin/env bash
	set -euo pipefail
	image=$(docker compose config --images dns)
	if [[ "$image" != "caddy-local-dns:latest" ]]; then
		docker compose pull --quiet dns && exit 0
		echo "Couldn't pull the dns image, so building it here instead. Check DNS_IMAGE and your registry login." >&2
	fi
	docker compose build dns

# One-off: let the unprivileged dns-sync container write resolver files
_resolver-dir:
    #!/usr/bin/env bash
    set -euo pipefail
    [[ -w "{{resolver_dir}}" ]] && exit 0
    if [[ -e "{{resolver_dir}}" ]]; then
        echo "{{resolver_dir}} already exists and isn't writable by you. Its files:"
        ls -l "{{resolver_dir}}"
        read -rp "Take ownership of it so dns-sync can add and remove its own files? (y/n) " answer
        if [[ ! "$answer" =~ ^[Yy] ]]; then
            echo "Skipped. dns-sync can't write there, so label domains won't resolve." >&2
            exit 0
        fi
    else
        echo "Creating {{resolver_dir}} owned by $USER, so domains can be added without sudo (password required):"
    fi
    sudo mkdir -p "{{resolver_dir}}"
    sudo chown "$USER" "{{resolver_dir}}"

_maybe-trust: _wait-for-cert
    #!/usr/bin/env bash
    set -euo pipefail
    fingerprint=$(openssl x509 -noout -fingerprint -sha1 -in "{{cert}}" | cut -d= -f2 | tr -d :)
    if security find-certificate -a -Z -c "{{ca_name}}" "{{keychain}}" 2>/dev/null | grep -q "SHA-1 hash: $fingerprint"; then
        echo "Root certificate already trusted."
        exit 0
    fi
    read -rp "This Caddy's root certificate isn't trusted yet. Add it to the keychain now? (y/n) " answer
    if [[ "$answer" =~ ^[Yy] ]]; then
        just trust
    else
        echo "Skipped. Run 'just trust' later, or manually trust {{cert}}."
    fi

_wait-for-cert:
    #!/usr/bin/env bash
    for _ in $(seq 1 30); do
        [[ -f "{{cert}}" ]] && exit 0
        sleep 1
    done
    echo "Timed out waiting for {{cert}} - is Caddy running? (just up)" >&2
    exit 1
