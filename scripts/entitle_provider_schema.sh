#!/usr/bin/env bash
# Regenerate tests/fixtures/entitle_provider_schema.json from the entitleio/entitle
# provider's own schema (`terraform providers schema -json`).
#
# The fixture is what tests/test_entitle_virtual_app.py checks every attribute the HCL
# generators emit against — the guard against writing HCL from the docs, which is how
# entitle_user_jit once shipped with almost no real attributes (f2610a6).
#
# Builds the provider from source through the Go module proxy (no registry or GitHub
# access needed), so it needs Go and terraform on PATH. REV defaults to master.
#
# Usage: scripts/entitle_provider_schema.sh [REV]
set -euo pipefail
cd "$(dirname "$0")/.."
rev=${1:-master}
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mod=github.com/entitleio/terraform-provider-entitle
ver=$(curl -fsS "https://proxy.golang.org/$mod/@v/$rev.info" | python3 -c 'import json,sys;print(json.load(sys.stdin)["Version"])')
curl -fsS -o "$work/src.zip" "https://proxy.golang.org/$mod/@v/$ver.zip"
(cd "$work" && unzip -q src.zip)
(cd "$work/$mod@$ver" && GOFLAGS=-mod=mod go build -o "$work/bin/terraform-provider-entitle" .)
cat > "$work/tfrc" <<RC
provider_installation {
  dev_overrides { "entitleio/entitle" = "$work/bin" }
  direct {}
}
RC
mkdir "$work/cfg"
printf 'terraform {\n  required_providers {\n    entitle = { source = "entitleio/entitle" }\n  }\n}\n' > "$work/cfg/main.tf"
TF_CLI_CONFIG_FILE="$work/tfrc" terraform -chdir="$work/cfg" providers schema -json > "$work/schema.json"
python3 - "$work/schema.json" "$ver" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
p = list(d["provider_schemas"].values())[0]

def attrs(block):
    out = {}
    for k, a in (block.get("attributes") or {}).items():
        mode = "required" if a.get("required") else "optional" if a.get("optional") else "computed"
        e = {"mode": mode}
        if a.get("nested_type"):
            e["nested"] = attrs(a["nested_type"])
        out[k] = e
    for k, b in (block.get("block_types") or {}).items():
        out[k] = {"mode": "block", "nested": attrs(b["block"])}
    return out

fx = {"_source": f"entitleio/entitle provider built from {sys.argv[2]}; regenerate with scripts/entitle_provider_schema.sh",
      "resource": {k: attrs(p["resource_schemas"][k]["block"]) for k in
                   ("entitle_integration", "entitle_resource", "entitle_role", "entitle_bundle")},
      "data": {k: attrs(p["data_source_schemas"][k]["block"]) for k in
               ("entitle_workflow", "entitle_roles", "entitle_integration")}}
json.dump(fx, open("tests/fixtures/entitle_provider_schema.json", "w"), indent=1, sort_keys=True)
print("wrote tests/fixtures/entitle_provider_schema.json from", sys.argv[2])
PY
