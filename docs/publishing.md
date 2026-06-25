# Publishing Django Orbit

This is the release checklist for publishing Django Orbit to GitHub, PyPI and the public MkDocs site.

## Release Order

1. Merge the release PR into `main`.
2. Pull the final `main` locally and confirm the version.
3. Run the full test suite.
4. Build the package from a clean `dist/`.
5. Verify the built wheel and sdist.
6. Publish to PyPI.
7. Create the GitHub release and tag.
8. Deploy the documentation site.
9. Verify PyPI, GitHub release, docs and a fresh install.

## Preflight

```bash
git checkout main
git pull --ff-only
python -m pytest --tb=short -q
python -m pip install build twine
```

Confirm these files are aligned before publishing:

- `pyproject.toml` project version
- `orbit/__init__.py` `__version__`
- `CHANGELOG.md` release section
- `README.md` package landing page
- `docs/` and `mkdocs.yml` for user-visible changes

## Build

Clean old artifacts and build fresh distributions:

```bash
rm -rf dist/ build/ *.egg-info/
python -m build
python -m twine check dist/*
```

Expected files for version `X.Y.Z`:

- `dist/django_orbit-X.Y.Z.tar.gz`
- `dist/django_orbit-X.Y.Z-py3-none-any.whl`

## Publish to PyPI

```bash
python -m twine upload dist/django_orbit-X.Y.Z*
```

Use a PyPI project token when prompted:

```bash
python -m twine upload dist/django_orbit-X.Y.Z* -u __token__ -p pypi-...
```

## GitHub Release

Create a GitHub release from the same version and changelog section:

```bash
gh release create vX.Y.Z --target main --title "vX.Y.Z" --notes-file RELEASE_NOTES.md
```

The release notes should summarize:

- highlights for users;
- compatibility or migration notes;
- security/safety changes;
- test plan used for the release.

## Deploy Documentation

```bash
mkdocs build --strict
mkdocs gh-deploy
```

Documentation must be deployed from the same code that was released.

## Post-Publish Verification

Check the public package page:

```bash
python -m pip index versions django-orbit
```

Test a clean install in a temporary environment:

```bash
python -m venv .venv-release-check
.venv-release-check\Scripts\python -m pip install --upgrade pip
.venv-release-check\Scripts\python -m pip install "django-orbit[mcp]==X.Y.Z"
.venv-release-check\Scripts\python -c "import orbit; print(orbit.__version__)"
```

Verify these public URLs:

- PyPI: `https://pypi.org/project/django-orbit/`
- GitHub release: `https://github.com/astro-stack/django-orbit/releases/tag/vX.Y.Z`
- Docs: `https://astro-stack.github.io/django-orbit/`
- MCP docs: `https://astro-stack.github.io/django-orbit/mcp/`

## If Upload Fails

PyPI versions are immutable. If `X.Y.Z` was already uploaded, bump to the next patch version, update changelog/version files, rebuild and upload again.