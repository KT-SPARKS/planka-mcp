# Releasing

```bash
# 1. bump the version in three places
#    pyproject.toml, src/planka_mcp/__init__.py, src/planka_mcp/app.py (MCPServer version=)

# 2. test and build
.venv/bin/python -m pytest tests -q
rm -rf dist && uv build

# 3. tag and push
git commit -am "release: vX.Y.Z - ..."
git tag -a vX.Y.Z -m "planka-mcp vX.Y.Z - ..."
git push origin main --tags

# 4. publish, including the stable-named sdist that the auto-update URL points at
cp dist/planka_mcp-X.Y.Z.tar.gz /tmp/planka_mcp-latest.tar.gz
gh release create vX.Y.Z \
  dist/planka_mcp-X.Y.Z-py3-none-any.whl \
  dist/planka_mcp-X.Y.Z.tar.gz \
  /tmp/planka_mcp-latest.tar.gz \
  --title "vX.Y.Z" --notes "..."
```

**Do not skip `planka_mcp-latest.tar.gz`.** Every user configured for
auto-update pulls
`releases/latest/download/planka_mcp-latest.tar.gz`. A release without that
asset leaves them silently stuck on the previous version — GitHub's
`latest/download` path 404s when the newest release lacks the file.

The stable asset must be an **sdist**, not a wheel: wheel filenames are parsed
for a PEP 440 version, and `planka_mcp-latest-py3-none-any.whl` is rejected with
"invalid version". Sdists are not filename-checked the same way.

## Verifying a release

```bash
uvx --refresh-package planka-mcp \
  --from https://github.com/KT-SPARKS/planka-mcp/releases/latest/download/planka_mcp-latest.tar.gz \
  planka-mcp
```

With `PLANKA_BASE_URL` unset it should exit with a config error, which proves the
download, build and entry point all work.
