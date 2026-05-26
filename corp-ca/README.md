# corp-ca/

Drop corporate root CA certificates here if your network uses a TLS-inspecting
proxy (Cloudflare Gateway, Zscaler, Palo Alto, Netskope, etc.) that re-signs
outbound traffic. The Dockerfile copies any `.crt` or `.pem` files in this
directory into the image's system trust store before `pip install` and
`apt-get update` run, so the build works behind the proxy.

## Usage

1. Export your corporate root CA in PEM or DER format (DER files should be
   converted with `openssl x509 -inform DER -in <file>.cer -out <name>.crt`).
2. Drop the resulting file into this directory:

   ```
   corp-ca/
     corp-root.crt
   ```

3. Rebuild the image: `./scripts/onboard.sh --build`.

Files in this directory (other than `.gitkeep` and this README) are gitignored,
so you won't accidentally commit your corporate cert.

See [docs/ONBOARDING.md](../docs/ONBOARDING.md#wsl-docker-pull-fails-with-a-certificate-error)
for the full WSL setup, including how to export the cert from Windows.
